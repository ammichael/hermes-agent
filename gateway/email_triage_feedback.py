"""Email triage inline-feedback registry and callback sink.

The Email Triage cron registers one short-lived token per `needs_review`
email and sends inline buttons with callback_data `ef:<token>:<category>`.
Telegram callbacks resolve the token here and persist Mike's choice through the
AgenticOS email chat-feedback script.

Safety posture: callback side effects are local AgenticOS learning writes only
(`email-chat-feedback` / optional auto-promoted local rule). It never sends,
archives, labels, or mutates email remotely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


FEEDBACK_DIR = Path("agenticos-substrate/feedback")
REGISTRY_FILE = "email-triage-inline-registry.json"
EVENTS_FILE = "email-triage-inline-feedback.jsonl"
WORKSPACE = Path("/Volumes/git/personal/agenticos/workspace")
FEEDBACK_SCRIPT = WORKSPACE / "scripts" / "email_chat_feedback.py"
VALID_CATEGORIES = {"urgent", "normal", "archive", "spam"}
CATEGORY_LABELS = {
    "urgent": "🚨 urgente",
    "normal": "👀 normal",
    "archive": "🗄️ arquivar",
    "spam": "🧯 spam",
}


def _append_event(event: Mapping[str, Any]) -> Path:
    path = events_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _find_existing_event(token: str, category: str, message_id: str | None) -> dict[str, Any] | None:
    """Return a prior identical callback event, if Telegram retries the click."""
    path = events_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for raw in reversed(lines[-2000:]):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("token") != token or event.get("category") != category:
            continue
        callback = event.get("callback", {}) if isinstance(event.get("callback"), dict) else {}
        if message_id and str(callback.get("message_id") or "") != str(message_id):
            continue
        return event if isinstance(event, dict) else None
    return None


def _mark_review_resolved(entry: Mapping[str, Any], category: str, *, token: str) -> None:
    """Persist a per-thread resolution so the classifier stops re-asking."""
    thread_id = str(entry.get("thread_id") or entry.get("threadId") or "").strip() or token
    try:
        import sys

        scripts_dir = WORKSPACE / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from state_utils import load_agent_state, save_agent_state  # type: ignore

        state = load_agent_state("email-review-resolved")
        if not isinstance(state, dict):
            state = {}
        threads = state.setdefault("threads", {})
        if not isinstance(threads, dict):
            threads = {}
            state["threads"] = threads
        threads[thread_id] = {
            "category": category,
            "sender": str(entry.get("sender") or entry.get("from") or ""),
            "subject": str(entry.get("subject") or ""),
            "token": token,
            "resolved_at": utc_now_iso(),
        }
        save_agent_state("email-review-resolved", state)
    except Exception:
        # Feedback learning is more important than dedup bookkeeping; keep the
        # callback successful even if this auxiliary state write fails.
        return


@dataclass(frozen=True)
class EmailFeedbackResult:
    token: str
    category: str
    label: str
    sender: str
    subject: str
    thread_id: str
    event_path: Path
    script_output: str


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


def make_token(account: str, sender: str, subject: str, thread_id: str) -> str:
    raw = f"{account}\0{sender}\0{subject}\0{thread_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def register_email(entry: Mapping[str, Any]) -> str:
    """Register one review email and return a short callback token."""
    account = str(entry.get("account") or "").strip()
    sender = str(entry.get("from") or entry.get("sender") or "").strip()
    subject = str(entry.get("subject") or "").strip()
    thread_id = str(entry.get("thread_id") or entry.get("threadId") or "").strip()
    if not sender or not subject:
        raise ValueError("entry must include sender/from and subject")

    token = str(entry.get("token") or make_token(account, sender, subject, thread_id))
    registry = _load_json_object(registry_path())
    registry[token] = {
        **dict(entry),
        "token": token,
        "account": account,
        "sender": sender,
        "subject": subject,
        "thread_id": thread_id,
        "registered_at": str(entry.get("registered_at") or utc_now_iso()),
        "callback_prefix": f"ef:{token}:",
        "allowed_categories": sorted(VALID_CATEGORIES),
    }
    _atomic_write_json(registry_path(), registry)
    return token


def resolve_email(token: str) -> dict[str, Any] | None:
    entry = _load_json_object(registry_path()).get(token)
    return entry if isinstance(entry, dict) else None


def parse_callback_data(data: str) -> tuple[str, str]:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "ef" or not parts[1] or not parts[2]:
        raise ValueError("invalid email feedback callback_data")
    token, category = parts[1], parts[2]
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid email category: {category}")
    return token, category


def record_email_feedback(
    *,
    token: str,
    category: str,
    user_id: str | None = None,
    user_name: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    callback_data: str | None = None,
) -> EmailFeedbackResult:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid email category: {category}")
    entry = resolve_email(token)
    if not entry:
        raise KeyError(f"unknown email feedback token: {token}")

    sender = str(entry.get("sender") or entry.get("from") or "").strip()
    subject = str(entry.get("subject") or "").strip()
    email_thread_id = str(entry.get("thread_id") or entry.get("threadId") or "").strip()
    if not sender:
        raise ValueError("registered email entry has no sender")

    existing_event = _find_existing_event(token, category, message_id)
    if existing_event:
        _mark_review_resolved(entry, category, token=token)
        return EmailFeedbackResult(
            token=token,
            category=category,
            label=CATEGORY_LABELS[category],
            sender=sender,
            subject=subject,
            thread_id=email_thread_id,
            event_path=events_path(),
            script_output=str(existing_event.get("script_output") or "duplicate callback ignored"),
        )

    if not FEEDBACK_SCRIPT.exists():
        raise FileNotFoundError(str(FEEDBACK_SCRIPT))

    note = f"Mike classified thread {email_thread_id or token} via inline button"
    proc = subprocess.run(
        ["python3", str(FEEDBACK_SCRIPT), category, sender, "--note", note],
        cwd=str(WORKSPACE),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"email_chat_feedback.py exited {proc.returncode}")

    event = {
        "ts": utc_now_iso(),
        "token": token,
        "category": category,
        "label": CATEGORY_LABELS[category],
        "email": entry,
        "callback": {
            "user_id": user_id,
            "user_name": user_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "callback_data": callback_data,
        },
        "script_output": output,
        "safety": {
            "external_writes": False,
            "email_mutated": False,
            "scope": "local AgenticOS email-chat-feedback learning only",
        },
    }
    path = _append_event(event)
    _mark_review_resolved(entry, category, token=token)

    return EmailFeedbackResult(
        token=token,
        category=category,
        label=CATEGORY_LABELS[category],
        sender=sender,
        subject=subject,
        thread_id=email_thread_id,
        event_path=path,
        script_output=output,
    )
