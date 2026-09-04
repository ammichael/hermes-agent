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
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    """``True`` quando gravou. Uma revisão que regride é ignorada, não é erro."""
    reminder_id = str(ack.get("reminder_id") or "")
    instance_key = str(ack.get("instance_key") or "")
    outcome = str(ack.get("outcome") or "")
    try:
        revision = int(ack.get("revision") or 0)
    except (TypeError, ValueError):
        return False
    if not reminder_id or not instance_key or revision <= 0 or outcome not in _ACK_RANK:
        return False

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = {}
    key = f"{reminder_id}|{instance_key}"
    existing = stored.get(key)
    if isinstance(existing, dict):
        existing_revision = int(existing.get("revision") or 0)
        if revision < existing_revision:
            return False
        if revision == existing_revision and _ACK_RANK[outcome] < _ACK_RANK.get(
            str(existing.get("outcome") or ""), 0
        ):
            return False

    stored[key] = {
        "reminder_id": reminder_id,
        "instance_key": instance_key,
        "revision": revision,
        "outcome": outcome,
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return True


REGISTRATION_PATH = HERMES_ROOT / "companion" / "apns-registration.json"
START_CLAIMS_DIR = HERMES_ROOT / "companion" / "live-activity-start-claims"

_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,256}$")


def record_activity_token(
    token: str,
    *,
    path: Path = REGISTRATION_PATH,
    claims_dir: Path = START_CLAIMS_DIR,
) -> bool:
    """Grava o token da Live Activity e solta o claim de start que o travava.

    Sem o token, o script do banner não consegue `update` e cai no `start`; com
    o claim em `confirmed` ele então difere o start e devolve sucesso sem postar
    nada. Renovar o token sem soltar o claim manteria o deadlock de pé.
    """
    if not _HEX_TOKEN_RE.match(token or ""):
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    if stored.get("activityToken") == token:
        return True

    stored["activityToken"] = token
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)

    # Um token novo significa Activity nova: todo claim de start anterior descreve
    # uma Activity que não existe mais.
    try:
        for claim in claims_dir.iterdir():
            if claim.is_file() and claim.suffix not in {".lock"}:
                claim.unlink(missing_ok=True)
    except OSError:
        pass
    return True
