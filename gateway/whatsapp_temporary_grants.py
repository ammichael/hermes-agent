"""Fail-closed reader for outbound-created WhatsApp interaction grants."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.whatsapp_identity import normalize_whatsapp_identifier
from hermes_constants import get_hermes_home


def temporary_grants_path() -> Path:
    return get_hermes_home() / "state" / "whatsapp-temporary-interactions.json"


def _jid(value: object) -> str:
    raw = str(value or "").strip().lower()
    identifier = normalize_whatsapp_identifier(raw)
    if not identifier:
        return ""
    suffix = "@lid" if raw.endswith("@lid") else "@s.whatsapp.net"
    return f"{identifier}{suffix}"


def _future(value: object, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            return False
        return expires.astimezone(timezone.utc) > now
    except (TypeError, ValueError):
        return False


def find_temporary_interaction_grant(
    source: SessionSource,
    *,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the exact active text-only DM grant for *source*, else ``None``."""
    if (
        source.platform != Platform.WHATSAPP
        or source.chat_type != "dm"
        or not source.chat_id
        or not source.user_id
    ):
        return None
    try:
        data = json.loads((state_path or temporary_grants_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return None

    chat_jid = _jid(source.chat_id)
    sender_jid = _jid(source.user_id)
    current = now or datetime.now(timezone.utc)
    for grant in data.get("grants") or []:
        if not isinstance(grant, dict):
            continue
        participants = {_jid(value) for value in grant.get("participant_jids") or []}
        if (
            grant.get("kind") == "temporary_interaction"
            and grant.get("enabled") is True
            and grant.get("capabilities") == ["text_reply"]
            and bool(str(grant.get("topic") or "").strip())
            and bool(grant.get("delivery_message_ids"))
            and _jid(grant.get("chat_jid")) == chat_jid
            and sender_jid in participants
            and _future(grant.get("expires_at"), current)
        ):
            return grant
    return None
