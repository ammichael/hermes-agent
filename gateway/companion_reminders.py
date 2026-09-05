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
import re
import subprocess
import sys
import threading
from pathlib import Path
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

# O script chama outros dois com timeouts de 45 s e 60 s, e ainda espera um lock
# de estado. Dois minutos é folga sobre o pior caso medido, não um chute.
ACTION_TIMEOUT_SECONDS = 120


def build_action_argv(
    reminder_id: str,
    kind: str,
    taken_at: Optional[str],
    instance_key: Optional[str],
) -> List[str]:
    """Argumentos como lista. Nunca uma string de shell, nunca ``shell=True``."""
    argv = [
        sys.executable,
        str(ACTION_SCRIPT),
        kind,
        "--id",
        reminder_id,
        "--source",
        "live_activity",
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
) -> Dict[str, Any]:
    """Executa a ação fora do event loop — o script faz I/O de arquivo sob lock."""
    argv = build_action_argv(reminder_id, kind, taken_at, instance_key)
    return await asyncio.to_thread(_run_blocking, argv)


def validate_action_request(
    reminder_id: str, payload: Dict[str, Any]
) -> Optional[str]:
    """Retorna a mensagem de erro, ou ``None`` quando a entrada serve."""
    if not REMINDER_ID_RE.match(reminder_id or ""):
        return "invalid_reminder_id"
    kind = payload.get("kind")
    if kind not in VALID_KINDS:
        return "invalid_kind"
    instance_key = payload.get("instance_key")
    if instance_key is not None and not INSTANCE_KEY_RE.match(str(instance_key)):
        return "invalid_instance_key"
    return None


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
