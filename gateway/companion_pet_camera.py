"""Resolve token-gated pet-camera notification stills for Companion rich push."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("EUFY_PET_WATCH_STATE_DIR") or (HOME / ".hermes" / "state" / "eufy-pet-watch"))
TOKENS = ROOT / "notification-media"
INBOX = Path(os.environ.get("EUFY_PET_WATCH_INBOX_DIR") or (HOME / "Pictures" / "N" / "pet-camera" / "inbox"))


def media_path(event_id: str, token: str) -> Path | None:
    if not event_id or not token or len(token) != 64:
        return None
    meta = TOKENS / f"{event_id}.json"
    if not meta.is_file():
        return None
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expect = str(payload.get("token_sha256") or "")
    got = hashlib.sha256(token.encode()).hexdigest()
    if not expect or got != expect:
        return None
    raw = Path(str(payload.get("path") or ""))
    try:
        path = raw.resolve()
    except OSError:
        return None
    # Allow inbox or explicit path under Pictures/N/pet-camera
    allowed_roots = [INBOX.resolve(), (HOME / "Pictures" / "N" / "pet-camera").resolve()]
    if not path.is_file():
        return None
    if path.stat().st_size > 8 * 1024 * 1024:
        return None
    ok = False
    for root in allowed_roots:
        try:
            path.relative_to(root)
            ok = True
            break
        except ValueError:
            continue
    return path if ok else None
