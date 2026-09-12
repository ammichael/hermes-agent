"""Rotas de lembrete must-confirm para o Companion iOS.

O telefone precisa responder um lembrete sem o app do Mac aberto. Toda a
lógica de estado continua em ``~/.hermes/scripts/must-confirm-live-action.py``,
que carrega o lock, publica o plano terminal e sincroniza o WhatsApp: aqui só
existe validação de entrada e execução. Reescrever aquilo em Python novo seria
duplicar a única cópia testada dessas regras.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils import atomic_json_write
from hermes_cli.auth import _file_lock
from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

# `get_default_hermes_root()`, e não `Path.home() / ".hermes"`, nem
# `get_hermes_home()`.
#
# Contra `Path.home()`: o `conftest.py` da suíte isola HERMES_HOME mas
# deliberadamente NÃO isola HOME, e diz por escrito que um path derivado de
# `Path.home() / ".hermes"` é "a bug to fix at the callsite". Com o default
# saindo de HOME, um teste que esquecesse de passar `claims_dir=` varria o
# `~/.hermes/companion/live-activity-start-claims` DO USUÁRIO — e apagou os
# claims de verdade, uma vez por `pytest tests/gateway/`.
#
# Contra `get_hermes_home()`: sob um gateway de profile (existem dois nesta
# máquina, `HERMES_HOME=~/.hermes/profiles/{finaya,tibiaura}`) ele devolveria
# `<profile>/companion/apns-registration.json`, enquanto
# `~/.hermes/scripts/companion-live-activity-banner.py:34` lê
# `Path.home()/".hermes"/"companion"` fixo. O gateway gravaria o token num
# arquivo que o publicador do banner nunca abre. `get_default_hermes_root()`
# devolve a RAIZ (`~/.hermes`) nos dois casos — idêntico ao comportamento de
# hoje em produção — e o tempdir sob pytest, que é o ponto.
HERMES_ROOT = get_default_hermes_root()
ACTION_SCRIPT = HERMES_ROOT / "scripts" / "must-confirm-live-action.py"

VALID_KINDS = {"done", "skip", "snooze10", "snooze15"}
REMINDER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
INSTANCE_KEY_RE = re.compile(r"^[A-Za-z0-9._:+-]{1,120}$")

# Companion iOS PR #3: pet-camera identification taps reuse the reminder-action
# route with reminder_id = pet:{eventId} and kind/label in {maju,nemo,none,both}.
# These never mutate must-confirm state; they forward to Laura via n-bridge.
PET_CAMERA_REMINDER_RE = re.compile(r"^pet:20\d{6}T\d{6}Z$")
PET_CAMERA_EVENT_RE = re.compile(r"^20\d{6}T\d{6}Z$")
PET_CAMERA_KINDS = frozenset({"maju", "nemo", "none", "both", "no_image"})
LAURA_AGENT_ID = "36e79656-9a36-4658-b303-544b51bb3bef"
GROK_N_AGENT_ID = "15c69e7c-7597-41da-9010-04418b0828e1"
PET_WATCH_STATE = HERMES_ROOT / "state" / "eufy-pet-watch"
VISUAL_FEEDBACK_PATH = PET_WATCH_STATE / "visual-feedback.jsonl"
LAURA_PENDING_PATH = PET_WATCH_STATE / "laura-training-pending.json"
INTERACTION_CLAIM_SCRIPT = HERMES_ROOT / "scripts" / "companion-interaction-claim.py"
STAMP_PET_LABEL = HERMES_ROOT / "integrations" / "eufy-pet-watch" / "stamp-pet-label.js"

# O script chama outros dois com timeouts de 45 s e 60 s, e ainda espera um lock
# de estado. Dois minutos é folga sobre o pior caso medido, não um chute.
ACTION_TIMEOUT_SECONDS = 120


def build_action_argv(
    reminder_id: str,
    kind: str,
    taken_at: Optional[str],
    instance_key: Optional[str],
    source: str = "live_activity",
) -> List[str]:
    """Argumentos como lista. Nunca uma string de shell, nunca ``shell=True``."""
    argv = [
        sys.executable,
        str(ACTION_SCRIPT),
        kind,
        "--id",
        reminder_id,
        "--source",
        source,
    ]
    if taken_at:
        argv += ["--taken-at", taken_at]
    if instance_key:
        argv += ["--instance-key", instance_key]
    return argv


def _run_blocking(argv: List[str]) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=ACTION_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        # Timeout não é recusa: o telefone deve poder tentar de novo.
        return {"ok": False, "exit": -1, "error": "action_timeout", "retryable": True}
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        body = None
    if not isinstance(body, dict):
        # Todo caminho de saída do script imprime um objeto JSON, inclusive o
        # `except` de topo. Stdout vazio ou ilegível quer dizer que o processo
        # morreu antes de ter veredicto — e um processo morto não é um "não".
        # Sem esta separação o telefone lê `ok:false` + `retryable:false`, chama
        # de recusa, e o Feito do usuário some sem nem tentar o relay.
        logger.error(
            "must-confirm-live-action morreu sem veredicto: exit=%s stderr=%s",
            proc.returncode,
            (proc.stderr or "").strip()[-2000:] or "<vazio>",
        )
        return {
            "ok": False,
            "exit": proc.returncode,
            "error": "script_failed",
            "retryable": True,
        }
    body["exit"] = proc.returncode
    body.setdefault("ok", proc.returncode == 0)
    # Exit 1 e 2 são veredictos, não falhas de infraestrutura: reenviar repete o
    # mesmo veredicto e só gasta bateria.
    body["retryable"] = False
    return body


async def run_reminder_action(
    reminder_id: str,
    kind: str,
    taken_at: Optional[str] = None,
    instance_key: Optional[str] = None,
    source: str = "live_activity",
) -> Dict[str, Any]:
    """Executa a ação fora do event loop — o script faz I/O de arquivo sob lock."""
    argv = build_action_argv(reminder_id, kind, taken_at, instance_key, source)
    return await asyncio.to_thread(_run_blocking, argv)


async def list_today() -> Dict[str, Any]:
    """Use the native scheduler projection; never read or mutate a second schedule."""
    return await asyncio.to_thread(_run_blocking, [sys.executable, str(ACTION_SCRIPT), "list"])


def is_pet_camera_reminder(reminder_id: str) -> bool:
    return bool(PET_CAMERA_REMINDER_RE.fullmatch(reminder_id or ""))


def pet_camera_event_id(reminder_id: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """Prefer explicit pet_camera_event_id; else strip the ``pet:`` prefix."""
    if payload:
        raw = payload.get("pet_camera_event_id")
        if isinstance(raw, str) and PET_CAMERA_EVENT_RE.fullmatch(raw):
            return raw
    if is_pet_camera_reminder(reminder_id):
        return reminder_id.split(":", 1)[1]
    return ""


def validate_action_request(
    reminder_id: str, payload: Dict[str, Any]
) -> Optional[str]:
    """Retorna a mensagem de erro, ou ``None`` quando a entrada serve."""
    if is_pet_camera_reminder(reminder_id):
        kind = payload.get("kind")
        label = payload.get("label", kind)
        if not isinstance(kind, str) or kind not in PET_CAMERA_KINDS:
            return "invalid_kind"
        if not isinstance(label, str) or label not in PET_CAMERA_KINDS:
            return "invalid_kind"
        if label != kind:
            return "invalid_kind"
        event_id = pet_camera_event_id(reminder_id, payload)
        if not PET_CAMERA_EVENT_RE.fullmatch(event_id):
            return "invalid_reminder_id"
        if event_id != reminder_id.split(":", 1)[1]:
            return "invalid_reminder_id"
        taken_at = payload.get("taken_at")
        if taken_at is not None and (not isinstance(taken_at, str) or not taken_at):
            return "missing_action_context"
        request_id = payload.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not request_id):
            return "invalid_request_id"
        return None
    if not REMINDER_ID_RE.match(reminder_id or ""):
        return "invalid_reminder_id"
    source = payload.get("source", "live_activity")
    if not isinstance(source, str) or source not in {"live_activity", "companion_app"}:
        return "invalid_source"
    if source == "companion_app" and (
        not isinstance(payload.get("taken_at"), str) or not payload["taken_at"]
        or not isinstance(payload.get("instance_key"), str) or not payload["instance_key"]
    ):
        return "missing_action_context"
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in VALID_KINDS:
        return "invalid_kind"
    instance_key = payload.get("instance_key")
    if instance_key is not None and not INSTANCE_KEY_RE.match(str(instance_key)):
        return "invalid_instance_key"
    return None



def _load_interaction_claim_mod():
    """Import companion-interaction-claim for n-bridge emit (no claim required)."""
    import importlib.util

    if not INTERACTION_CLAIM_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "companion_interaction_claim", INTERACTION_CLAIM_SCRIPT
    )
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stamp_pet_camera_feedback(event_id: str, label: str) -> Dict[str, Any]:
    """Best-effort visual-feedback + laura-pending stamp. Never raises to callers."""
    out: Dict[str, Any] = {"visual_feedback": False, "laura_pending": False}
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    try:
        PET_WATCH_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        row = {
            "eventId": event_id,
            "feedback": label,
            "previousLabel": None,
            "label": label,
            "source": "mike_companion_push",
            "at": at,
        }
        # Fill previousLabel from laura pending or skip
        try:
            pending = json.loads(LAURA_PENDING_PATH.read_text(encoding="utf-8"))
            if isinstance(pending, dict) and pending.get("eventId") == event_id:
                row["previousLabel"] = pending.get("label")
        except (OSError, json.JSONDecodeError):
            pass
        with VISUAL_FEEDBACK_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        try:
            VISUAL_FEEDBACK_PATH.chmod(0o600)
        except OSError:
            pass
        out["visual_feedback"] = True
    except OSError as exc:
        out["visual_feedback_error"] = type(exc).__name__

    try:
        if LAURA_PENDING_PATH.is_file():
            pending = json.loads(LAURA_PENDING_PATH.read_text(encoding="utf-8"))
            if isinstance(pending, dict) and pending.get("eventId") == event_id:
                pending["status"] = "labeled"
                pending["label"] = label
                pending["labelSource"] = "mike_companion_push"
                pending["labeledAt"] = datetime.now(timezone.utc).isoformat()
                if pending.get("pushActionWait") == "waiting":
                    pending["pushActionWait"] = "resolved"
                    pending["pushActionResolvedAt"] = pending["labeledAt"]
                    pending["pushActionResolveReason"] = f"labeled_via_companion_push_{label}"
                atomic_json_write(LAURA_PENDING_PATH, pending, mode=0o600)
                out["laura_pending"] = True
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        out["laura_pending_error"] = type(exc).__name__
    if label != "no_image":
        out["stamp"] = _run_stamp_pet_label(event_id, label)
    else:
        out["stamp"] = {"ok": True, "skipped": "no_image"}
    return out


def _run_stamp_pet_label(event_id: str, label: str) -> Dict[str, Any]:
    """Album write via stamp-pet-label.js. Isolated by EUFY_PET_WATCH_STATE_DIR."""
    if not STAMP_PET_LABEL.is_file():
        return {"ok": False, "reason": "stamp-missing"}
    node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").is_file() else "node"
    env = dict(os.environ)
    env["STAMP_SOURCE"] = "mike_companion_push"
    env["EUFY_PET_WATCH_STATE_DIR"] = str(PET_WATCH_STATE)
    try:
        completed = subprocess.run(
            [node, str(STAMP_PET_LABEL), event_id, label],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        raw = (completed.stdout or completed.stderr or "").strip().splitlines()
        payload = {}
        if raw:
            try:
                payload = json.loads(raw[-1])
            except json.JSONDecodeError:
                payload = {"ok": False, "reason": "unparsed"}
        if completed.returncode != 0 and "ok" not in payload:
            payload = {"ok": False, "reason": payload.get("reason") or f"exit-{completed.returncode}"}
        return payload if isinstance(payload, dict) else {"ok": False, "reason": "invalid-stamp"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason": type(exc).__name__}


def _emit_pet_camera_n_bridge(event: Dict[str, Any]) -> Dict[str, Any]:
    mod = _load_interaction_claim_mod()
    if mod is not None and hasattr(mod, "emit_n_bridge"):
        return mod.emit_n_bridge(event)
    # Fallback mirror of companion-interaction-claim.emit_n_bridge inbox write
    inbox = HERMES_ROOT / "agenticos" / "n-bridge" / "to-grok"
    inbox.mkdir(parents=True, exist_ok=True)
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "from": "hermes-n",
        "to": "grok-n",
        "type": "message",
        "body": json.dumps(event, ensure_ascii=False),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = inbox / f"{msg_id}.json"
    atomic_json_write(path, msg, mode=0o600)
    return {"ok": True, "id": msg_id, "path": str(path), "wake": "skip_no_send_script"}


def build_pet_camera_event(
    reminder_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    label = str(payload.get("label") or payload.get("kind"))
    event_id = pet_camera_event_id(reminder_id, payload)
    return {
        "type": "companion.pet_camera_identification",
        "reminder_id": reminder_id,
        "eventId": event_id,
        "pet_camera_event_id": event_id,
        "action": label,
        "label": label,
        "kind": label,
        "owner_agent": LAURA_AGENT_ID,
        "owner_agent_raw": "laura",
        "source": "companion_push",
        "request_id": payload.get("request_id"),
        "taken_at": payload.get("taken_at"),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reply_via": "n-bridge",
        "route_hint": (
            f"Pet-camera ID tap for Laura ({LAURA_AGENT_ID}). "
            f"Grok N ({GROK_N_AGENT_ID}) should forward via SendToAgent when owner != n."
        ),
    }


def _run_pet_camera_identification_blocking(
    reminder_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    event = build_pet_camera_event(reminder_id, payload)
    stamps = _stamp_pet_camera_feedback(event["eventId"], event["label"])
    bridge = _emit_pet_camera_n_bridge(event)
    ok = bool(bridge.get("ok", True)) and bridge.get("error") is None
    return {
        "ok": ok,
        "action": event["label"],
        "label": event["label"],
        "reminder_id": reminder_id,
        "eventId": event["eventId"],
        "pet_camera": True,
        "retryable": not ok,
        "interaction_return": {
            "bridge_id": bridge.get("id"),
            "owner_agent": LAURA_AGENT_ID,
            "wake": bridge.get("wake"),
            "path": bridge.get("path"),
            "type": "companion.pet_camera_identification",
        },
        "stamps": stamps,
        "event": event,
    }


async def run_pet_camera_identification(
    reminder_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Accept a pet-camera ID tap without must-confirm mutation; notify Laura via n-bridge."""
    return await asyncio.to_thread(
        _run_pet_camera_identification_blocking, reminder_id, payload
    )


STATE_PATH = HERMES_ROOT / "agenticos" / "state" / "must-confirm-reminders.json"
ACK_PATH = HERMES_ROOT / "companion" / "reminder-plan-acks.json"

# Mesma ordem de `HermesReminderPlanRelay.acknowledgmentRank`: um resultado
# degradado não pode apagar um aplicado da mesma revisão.
_ACK_RANK = {"applied": 2, "noop": 2, "ignored_stale": 2, "degraded": 1, "rejected": 1}


def load_plans(*, path: Path = STATE_PATH) -> List[Dict[str, Any]]:
    """A revisão mais alta por instância, como o relay do Mac já faz."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    newest: Dict[str, Dict[str, Any]] = {}
    for plan in raw.get("companion_reminder_plan_outbox") or []:
        if not isinstance(plan, dict):
            continue
        key = f"{plan.get('reminder_id')}|{plan.get('instance_key')}"
        current = newest.get(key)
        if current is None or int(plan.get("revision") or 0) > int(current.get("revision") or 0):
            newest[key] = plan
    return [newest[k] for k in sorted(newest)]


def record_plan_ack(ack: Dict[str, Any], *, path: Path = ACK_PATH) -> bool:
    """Accept a durable ACK, including one already covered by a newer/stronger ACK."""
    reminder_id = str(ack.get("reminder_id") or "")
    instance_key = str(ack.get("instance_key") or "")
    outcome = str(ack.get("outcome") or "")
    revision = ack.get("revision")
    if (not REMINDER_ID_RE.fullmatch(reminder_id) or not INSTANCE_KEY_RE.fullmatch(instance_key)
            or type(revision) is not int or revision <= 0 or outcome not in _ACK_RANK):
        return False
    try:
        with _file_lock(path.with_suffix(".lock"), threading.local(), 5, "ACK store busy"):
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                stored = {}
            if not isinstance(stored, dict):
                return False
            key = f"{reminder_id}|{instance_key}"
            existing = stored.get(key)
            if isinstance(existing, dict):
                existing_revision = int(existing.get("revision") or 0)
                if revision < existing_revision:
                    return True
                if revision == existing_revision and _ACK_RANK[outcome] <= _ACK_RANK.get(
                    str(existing.get("outcome") or ""), 0
                ):
                    return True
            stored[key] = {
                "reminder_id": reminder_id, "instance_key": instance_key,
                "revision": revision, "outcome": outcome,
            }
            atomic_json_write(path, stored, mode=0o600)
            return True
    except (OSError, ValueError):
        return False


REGISTRATION_PATH = HERMES_ROOT / "companion" / "apns-registration.json"
START_CLAIMS_DIR = HERMES_ROOT / "companion" / "live-activity-start-claims"

_HEX_TOKEN_RE = re.compile(r"[0-9a-fA-F]{32,256}")
_REGISTRATION_FIELDS = {
    "activity_token": "activityToken", "push_to_start_token": "pushToStartToken",
    "device_token": "deviceToken", "environment": "environment",
}
BANNER_SCRIPT = HERMES_ROOT / "scripts" / "companion-live-activity-banner.py"
_COMMUNICATION_ID_RE = re.compile(r"[A-Za-z0-9:|@+._-]{1,128}")


def valid_push_registration(payload: Any) -> bool:
    if not isinstance(payload, dict) or not payload or payload.keys() - _REGISTRATION_FIELDS.keys():
        return False
    for key, value in payload.items():
        if key == "environment":
            if value not in ("sandbox", "production"):
                return False
        elif key == "activity_token" and value is None:
            continue
        elif not isinstance(value, str) or not _HEX_TOKEN_RE.fullmatch(value):
            return False
    return True


def record_push_registration(
    payload: dict, *, path: Path = REGISTRATION_PATH, claims_dir: Path = START_CLAIMS_DIR,
) -> bool:
    """Merge incremental phone tokens atomically; explicit null clears an ended activity."""
    if not valid_push_registration(payload):
        return False
    try:
        with _file_lock(path.with_suffix(".lock"), threading.local(), 5, "Registration store busy"):
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                stored = {}
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(stored, dict):
                return False
            release_claims = False
            for key, value in payload.items():
                field = _REGISTRATION_FIELDS[key]
                if field in ("activityToken", "pushToStartToken") and stored.get(field) != value:
                    release_claims = True
                if value is None:
                    stored.pop(field, None)
                else:
                    stored[field] = value
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_json_write(path, stored, mode=0o600)
            except OSError:
                return False
            if release_claims:
                try:
                    for claim in claims_dir.iterdir():
                        if claim.is_file() and claim.suffix != ".lock":
                            claim.unlink(missing_ok=True)
                except OSError:
                    pass
            return True
    except OSError:
        return False


def record_activity_token(
    token: str, *, path: Path = REGISTRATION_PATH, claims_dir: Path = START_CLAIMS_DIR,
) -> bool:
    return record_push_registration({"activity_token": token}, path=path, claims_dir=claims_dir)


def valid_communication_id(identifier: Any) -> bool:
    return isinstance(identifier, str) and bool(_COMMUNICATION_ID_RE.fullmatch(identifier))


async def dismiss_communication(identifier: str) -> dict:
    if not valid_communication_id(identifier):
        return {"ok": False, "error": "invalid_communication_id", "retryable": False}

    def run() -> dict:
        try:
            result = subprocess.run(
                [sys.executable, str(BANNER_SCRIPT), "dismiss", "--payload-stdin"],
                input=json.dumps({"id": identifier}), capture_output=True, text=True, timeout=45,
            )
            body = json.loads(result.stdout)
            if result.returncode == 0 and isinstance(body, dict) and body.get("ok") is True:
                return {"ok": True}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        return {"ok": False, "error": "dismiss_unavailable", "retryable": True}

    return await asyncio.to_thread(run)
