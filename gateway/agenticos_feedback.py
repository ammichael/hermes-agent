"""AgenticOS Telegram inline-feedback registry and event sink.

This module intentionally has no dependency on the Telegram SDK.  The Telegram
adapter calls it when it receives ``ago:<token>:<action>`` callback_data; local
AgenticOS scripts use it to register tokens before sending inline keyboards.

Safety posture for v0: callbacks only write inside the AgenticOS sandbox under
``~/.hermes/agenticos/substrate/feedback``.  No reminders, calendar, Notion, BuJo, cron,
or executor side effects happen here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - fallback for isolated tests
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


FEEDBACK_DIR = Path("agenticos-substrate/feedback")
REGISTRY_FILE = "telegram-inline-registry.json"
EVENTS_FILE = "telegram-inline-feedback.jsonl"

ACTION_SEMANTICS: dict[str, dict[str, str]] = {
    "useful": {
        "label": "👍 útil",
        "kind": "quality_signal",
        "score": "+2",
        "meaning": "Este candidato tem valor; aumente padrões parecidos.",
    },
    "maybe": {
        "label": "🟡 talvez",
        "kind": "quality_signal",
        "score": "+0.5",
        "meaning": "Pode ter valor, mas precisa de contexto/escopo melhor.",
    },
    "noise": {
        "label": "🧯 ruído",
        "kind": "quality_signal",
        "score": "-2",
        "meaning": "Não deveria ter aparecido; reduza padrões parecidos.",
    },
    "wrong": {
        "label": "❌ errado",
        "kind": "quality_signal",
        "score": "-4",
        "meaning": "Há erro factual/conceitual; corrigir parser/proveniência antes de confiar.",
    },
    "known": {
        "label": "✅ já sabia",
        "kind": "quality_signal",
        "score": "-0.5",
        "meaning": "Verdadeiro, mas óbvio/baixo valor incremental.",
    },
    "context": {
        "label": "🧩 falta contexto",
        "kind": "quality_signal",
        "score": "0",
        "meaning": "Sinal potencial, mas falta fonte/explicação para decidir.",
    },
    "sources": {
        "label": "🔎 fontes",
        "kind": "safe_action",
        "score": "0",
        "meaning": "Pedir/prover evidências e citações; não executa nada externo.",
    },
    "followup": {
        "label": "📌 follow-up shadow",
        "kind": "safe_action_request",
        "score": "+1",
        "meaning": "Registrar intenção de criar follow-up Kanban/local, ainda sem writes externos.",
    },
    "less": {
        "label": "🔕 menos disso",
        "kind": "preference_signal",
        "score": "-1",
        "meaning": "Reduzir frequência/peso deste padrão sem marcar como erro.",
    },
}


@dataclass(frozen=True)
class FeedbackResult:
    token: str
    action: str
    label: str
    meaning: str
    candidate_title: str
    run_id: str
    event_path: Path
    candidate: Mapping[str, Any]


def feedback_root() -> Path:
    root = get_hermes_home() / FEEDBACK_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def registry_path() -> Path:
    return feedback_root() / REGISTRY_FILE


def events_path() -> Path:
    return feedback_root() / EVENTS_FILE


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def make_token(run_id: str, candidate_id: str, title: str) -> str:
    raw = f"{run_id}\0{candidate_id}\0{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def register_candidate(entry: Mapping[str, Any]) -> str:
    """Register one candidate and return its short Telegram callback token.

    Required fields: ``run_id``, ``candidate_id``, ``title``.  Extra fields are
    persisted as metadata for later analysis.
    """
    run_id = str(entry.get("run_id") or "").strip()
    candidate_id = str(entry.get("candidate_id") or "").strip()
    title = str(entry.get("title") or "").strip()
    if not run_id or not candidate_id or not title:
        raise ValueError("entry must include run_id, candidate_id, and title")

    token = str(entry.get("token") or make_token(run_id, candidate_id, title))
    registry = _load_json_object(registry_path())
    registry[token] = {
        **dict(entry),
        "token": token,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "title": title,
        "registered_at": str(entry.get("registered_at") or utc_now_iso()),
        "callback_prefix": f"ago:{token}:",
        "allowed_actions": sorted(ACTION_SEMANTICS),
    }
    _atomic_write_json(registry_path(), registry)
    return token


def resolve_candidate(token: str) -> dict[str, Any] | None:
    entry = _load_json_object(registry_path()).get(token)
    return entry if isinstance(entry, dict) else None


def record_feedback(
    *,
    token: str,
    action: str,
    user_id: str | None = None,
    user_name: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    callback_data: str | None = None,
) -> FeedbackResult:
    if action not in ACTION_SEMANTICS:
        raise ValueError(f"unknown AgenticOS feedback action: {action}")
    entry = resolve_candidate(token)
    if not entry:
        raise KeyError(f"unknown AgenticOS feedback token: {token}")

    semantics = ACTION_SEMANTICS[action]
    event = {
        "ts": utc_now_iso(),
        "token": token,
        "action": action,
        "semantics": semantics,
        "candidate": entry,
        "telegram": {
            "user_id": user_id,
            "user_name": user_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "callback_data": callback_data,
        },
        "safety": {
            "external_writes": False,
            "executor_created": False,
            "cron_modified": False,
            "scope": "local agenticos-substrate feedback jsonl only",
        },
    }
    path = events_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    return FeedbackResult(
        token=token,
        action=action,
        label=semantics["label"],
        meaning=semantics["meaning"],
        candidate_title=str(entry.get("title") or token),
        run_id=str(entry.get("run_id") or ""),
        event_path=path,
        candidate=entry,
    )


def parse_callback_data(data: str) -> tuple[str, str]:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "ago" or not parts[1] or not parts[2]:
        raise ValueError("invalid AgenticOS callback_data")
    return parts[1], parts[2]
