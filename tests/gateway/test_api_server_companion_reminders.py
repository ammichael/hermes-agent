"""The companion reminder routes are mounted, authed, and validate input."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from gateway.platforms import api_server_companion_reminders as routes_mod


class _Request:
    def __init__(self, body, match_info=None):
        self._body = body
        self.match_info = match_info or {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _adapter(auth_response=None):
    return SimpleNamespace(_check_auth=lambda request: auth_response)


def test_route_table_exposes_the_phone_routes():
    table = routes_mod._http_routes(_adapter())
    assert [(m, p) for m, p, _ in table] == [
        ("POST", "/api/companion/reminders/{reminder_id}/action"),
        ("GET", "/api/companion/reminders/plans"),
        ("GET", "/api/companion/reminders/today"),
        ("POST", "/api/companion/reminders/plan-ack"),
        ("POST", "/api/companion/live-activity/token"),
        ("POST", "/api/companion/live-activity/dismiss"),
    ]
    assert all(callable(handler) for _, _, handler in table)


def test_auth_failure_short_circuits_before_reading_the_body():
    sentinel = object()
    handler = dict(((m, p), h) for m, p, h in routes_mod._http_routes(_adapter(auth_response=sentinel)))[
        ("POST", "/api/companion/reminders/{reminder_id}/action")
    ]
    assert asyncio.run(handler(_Request(ValueError("must not be read"), {"reminder_id": "lexa"}))) is sentinel


def test_action_rejects_bad_json_and_bad_kind_without_running_the_script():
    handler = dict(((m, p), h) for m, p, h in routes_mod._http_routes(_adapter()))[
        ("POST", "/api/companion/reminders/{reminder_id}/action")
    ]
    with patch.object(routes_mod._companion_reminders, "run_reminder_action") as run:
        bad_json = asyncio.run(handler(_Request(ValueError("boom"), {"reminder_id": "lexa"})))
        assert bad_json.status == 400 and json.loads(bad_json.text)["error"] == "invalid_json"
        bad_kind = asyncio.run(handler(_Request({"kind": "explode"}, {"reminder_id": "lexa"})))
        assert bad_kind.status == 400 and json.loads(bad_kind.text)["error"] == "invalid_kind"
        run.assert_not_called()


def test_action_verdict_is_returned_as_200_even_when_refused():
    handler = dict(((m, p), h) for m, p, h in routes_mod._http_routes(_adapter()))[
        ("POST", "/api/companion/reminders/{reminder_id}/action")
    ]

    async def fake_run(reminder_id, kind, taken_at=None, instance_key=None, source="live_activity"):
        return {"ok": False, "error": "stale_action", "retryable": False, "reminder_id": reminder_id}

    with patch.object(routes_mod._companion_reminders, "run_reminder_action", side_effect=fake_run):
        response = asyncio.run(
            handler(
                _Request({"kind": "done", "instance_key": "2026-09-04", "taken_at": "2026-09-04T10:00:00-03:00"}, {"reminder_id": "bedtime-lexa"})
            )
        )
    assert response.status == 200
    assert json.loads(response.text) == {"ok": False, "error": "stale_action", "retryable": False, "reminder_id": "bedtime-lexa"}


def test_today_is_authenticated_and_unavailability_is_not_an_empty_day():
    sentinel = object()
    with patch.object(routes_mod._companion_reminders, "list_today") as listing:
        result = asyncio.run(routes_mod._handle_reminders_today(_adapter(sentinel), _Request(None)))
        assert result is sentinel
        listing.assert_not_called()
    for body, status in [({"ok": True, "reminders": []}, 200),
                         ({"ok": False, "error": "script_failed"}, 503)]:
        async def fake_list():
            return body
        with patch.object(routes_mod._companion_reminders, "list_today", side_effect=fake_list):
            response = asyncio.run(routes_mod._handle_reminders_today(_adapter(), _Request(None)))
        assert response.status == status
        assert json.loads(response.text) == body


def test_action_source_is_preserved_and_privileged_sources_are_refused():
    for source in ("api", "visual_evidence", ["companion_app"]):
        with patch.object(routes_mod._companion_reminders, "run_reminder_action") as run:
            response = asyncio.run(routes_mod._handle_reminder_action(
                _adapter(), _Request({"kind": "done", "source": source}, {"reminder_id": "test-only"})))
            assert response.status == 400
            run.assert_not_called()
    async def fake_run(reminder_id, kind, **kwargs):
        assert kwargs["source"] == "companion_app"
        return {"ok": True}
    with patch.object(routes_mod._companion_reminders, "run_reminder_action", side_effect=fake_run):
        response = asyncio.run(routes_mod._handle_reminder_action(
            _adapter(), _Request({"kind": "done", "source": "companion_app"}, {"reminder_id": "test-only"})))
        assert json.loads(response.text)["ok"] is True
