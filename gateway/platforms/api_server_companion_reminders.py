"""``/api/companion/reminders/*`` — the iPhone answers a reminder without the Mac app.

Restored 2026-09-04 from local commits erased by ``hermes update`` (2026-08-11
``ffabffc8ea``..``4711bce33a``). Handlers live here, not on ``APIServerAdapter``, so
the core diff is one import and one ``routes.extend`` line. Business rules stay in
``gateway.companion_reminders`` and, behind it, ``~/.hermes/scripts/must-confirm-live-action.py``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - aiohttp is optional at import time
    web = None  # type: ignore[assignment]

from gateway import companion_reminders as _companion_reminders


async def _json_object(request: "web.Request") -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


async def _handle_reminder_action(adapter, request: "web.Request") -> "web.Response":
    """POST /api/companion/reminders/{id}/action — Feito/Adiar/Pular do iPhone."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    payload = await _json_object(request)
    if payload is None:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    reminder_id = request.match_info.get("reminder_id", "")
    invalid = _companion_reminders.validate_action_request(reminder_id, payload)
    if invalid:
        return web.json_response({"ok": False, "error": invalid}, status=400)
    result = await _companion_reminders.run_reminder_action(
        reminder_id,
        str(payload["kind"]),
        taken_at=payload.get("taken_at"),
        instance_key=payload.get("instance_key"),
    )
    # 200 even when `ok` is false: a Hermes refusal is a valid verdict, not a
    # protocol error. The phone reads `ok` (and `retryable` on process death).
    return web.json_response(result)


async def _handle_reminder_plans(adapter, request: "web.Request") -> "web.Response":
    """GET /api/companion/reminders/plans — the phone pulls instead of waiting for a push."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    plans = await asyncio.to_thread(_companion_reminders.load_plans)
    return web.json_response({"plans": plans})


async def _handle_reminder_plan_ack(adapter, request: "web.Request") -> "web.Response":
    """POST /api/companion/reminders/plan-ack — revision CAS reported by the phone."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    payload = await _json_object(request)
    if payload is None:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    recorded = await asyncio.to_thread(_companion_reminders.record_plan_ack, payload)
    return web.json_response({"ok": bool(recorded)})


async def _handle_activity_token(adapter, request: "web.Request") -> "web.Response":
    """POST /api/companion/live-activity/token — the phone renews its Live Activity push token."""
    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    payload = await _json_object(request)
    if payload is None:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    recorded = await asyncio.to_thread(
        _companion_reminders.record_activity_token, str(payload.get("activity_token") or "")
    )
    return web.json_response({"ok": bool(recorded)})


_ROUTES: tuple[tuple[str, str, Callable[..., Awaitable[Any]]], ...] = (
    ("POST", "/api/companion/reminders/{reminder_id}/action", _handle_reminder_action),
    ("GET", "/api/companion/reminders/plans", _handle_reminder_plans),
    ("POST", "/api/companion/reminders/plan-ack", _handle_reminder_plan_ack),
    ("POST", "/api/companion/live-activity/token", _handle_activity_token),
)


def _http_routes(adapter) -> list[tuple[str, str, Any]]:
    """Bind the module handlers to one adapter; mirrors ``api_server_runs._http_routes``."""

    def bind(fn):
        async def handler(request: "web.Request") -> "web.Response":
            return await fn(adapter, request)

        handler.__name__ = fn.__name__
        handler.__doc__ = fn.__doc__
        return handler

    return [(method, path, bind(fn)) for method, path, fn in _ROUTES]
