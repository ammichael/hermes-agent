"""Assign Hermes sessions to Companion conversation groups."""

import json
from typing import Any

from tools.registry import registry, tool_error


def _base_title(title: str) -> str:
    if title.casefold().startswith("n / ") and "·" in title:
        return title.split("·", 1)[1].strip()
    return title.strip()


def conversation_group(
    *, action: str, session_ids: list[str], group: str = "", db: Any = None
) -> dict[str, Any]:
    if action not in {"assign", "remove"}:
        return tool_error("action must be assign or remove")
    clean_group = " ".join(group.split())
    if action == "assign" and (not clean_group or len(clean_group) > 24 or "/" in clean_group or "·" in clean_group):
        return tool_error("group must be 1-24 characters without '/' or '·'")
    ids = list(dict.fromkeys(str(value).strip() for value in session_ids if str(value).strip()))
    if not ids or len(ids) > 50:
        return tool_error("session_ids must contain 1-50 exact session ids")

    if db is None:
        from hermes_state import SessionDB
        db = SessionDB()

    changed = []
    missing = []
    for session_id in ids:
        session = db.get_session(session_id)
        if not session:
            missing.append(session_id)
            continue
        base = _base_title(str(session.get("title") or "Conversa"))
        title = base if action == "remove" else f"N / {clean_group} · {base}"
        if db.set_session_title(session_id, title[:60]):
            changed.append(session_id)

    return json.dumps({
        "success": not missing,
        "action": action,
        "group": clean_group if action == "assign" else None,
        "changed_session_ids": changed,
        "missing_session_ids": missing,
    }, ensure_ascii=False)


CONVERSATION_GROUP_SCHEMA = {
    "name": "conversation_group",
    "description": (
        "Organize existing Hermes sessions into the thematic circles shown in the "
        "Companion app. Use session_search first to resolve exact session IDs. "
        "Call assign when the user asks N to create/use a theme or move conversations; "
        "call remove to return conversations to the ungrouped timeline."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["assign", "remove"]},
            "session_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 50,
            },
            "group": {
                "type": "string",
                "description": "Short visible theme, optionally starting with an emoji.",
            },
        },
        "required": ["action", "session_ids"],
    },
}


registry.register(
    name="conversation_group",
    toolset="conversation_group",
    schema=CONVERSATION_GROUP_SCHEMA,
    handler=lambda args, **kw: conversation_group(
        action=args.get("action", ""),
        session_ids=args.get("session_ids") or [],
        group=args.get("group", ""),
        db=kw.get("db"),
    ),
    check_fn=lambda: True,
    emoji="🗂️",
)
