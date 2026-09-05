"""Mobile registration and banner actions never need the desktop process."""
import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import companion_reminders as control
from gateway.platforms import api_server_companion_reminders as routes
from types import SimpleNamespace


def test_incremental_registration_preserves_fields_and_explicitly_clears_activity(tmp_path):
    path, claims = tmp_path / "registration.json", tmp_path / "claims"
    assert control.record_push_registration(
        {"device_token": "a" * 64, "environment": "sandbox", "push_to_start_token": "b" * 160},
        path=path, claims_dir=claims,
    )
    assert control.record_push_registration({"activity_token": "c" * 160}, path=path, claims_dir=claims)
    assert control.record_push_registration({"activity_token": None}, path=path, claims_dir=claims)
    assert json.loads(path.read_text()) == {
        "deviceToken": "a" * 64, "environment": "sandbox", "pushToStartToken": "b" * 160,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("payload", [
    {}, {"device_token": "not-hex"}, {"device_token": None},
    {"device_token": "a" * 64, "environment": "typo"},
    {"device_token": "a" * 64, "activity_token": True},
    {"activity_token": "a" * 160, "unexpected": "value"},
    {"push_to_start_token": "a" * 160 + "\n"},
])
def test_invalid_registration_cannot_partially_replace_existing_tokens(payload, tmp_path):
    path = tmp_path / "registration.json"
    before = '{"deviceToken":"d","environment":"production"}'
    path.write_text(before)
    assert not control.record_push_registration(payload, path=path, claims_dir=tmp_path / "claims")
    assert path.read_text() == before


def test_concurrent_registrations_do_not_lose_other_tokens(tmp_path):
    path, claims = tmp_path / "registration.json", tmp_path / "claims"
    payloads = [{"device_token": "a" * 64, "environment": "production"},
                {"push_to_start_token": "b" * 160}, {"activity_token": "c" * 160}]
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert all(pool.map(lambda p: control.record_push_registration(p, path=path, claims_dir=claims), payloads))
    assert set(json.loads(path.read_text())) == {"deviceToken", "environment", "pushToStartToken", "activityToken"}


def test_rotated_push_to_start_token_releases_old_claims(tmp_path):
    path, claims = tmp_path / "registration.json", tmp_path / "claims"
    path.write_text(json.dumps({"pushToStartToken": "a" * 160}))
    claims.mkdir()
    (claims / "old").write_text("confirmed")
    assert control.record_push_registration({"push_to_start_token": "b" * 160}, path=path, claims_dir=claims)
    assert not (claims / "old").exists()


class Request:
    def __init__(self, body): self.body = body
    async def json(self): return self.body


@pytest.mark.parametrize("path", ["/api/companion/live-activity/token", "/api/companion/live-activity/dismiss"])
def test_control_routes_authenticate_before_body(path):
    sentinel = object()
    adapter = SimpleNamespace(_check_auth=lambda _: sentinel)
    handler = {(m, p): h for m, p, h in routes._http_routes(adapter)}[("POST", path)]
    assert asyncio.run(handler(object())) is sentinel


def test_registration_route_rejects_bad_payload_without_returning_tokens():
    adapter = SimpleNamespace(_check_auth=lambda _: None)
    response = asyncio.run(routes._handle_activity_token(adapter, Request({"device_token": "private-invalid-token"})))
    assert response.status == 400
    assert "private-invalid-token" not in response.text


@pytest.mark.parametrize("identifier", ["", "x" * 129, "a\n", "../../etc/passwd", "a;echo x"])
def test_dismiss_rejects_invalid_ids_before_subprocess(identifier):
    adapter = SimpleNamespace(_check_auth=lambda _: None)
    response = asyncio.run(routes._handle_activity_dismiss(adapter, Request({"communication_id": identifier})))
    assert response.status == 400


def test_dismiss_uses_existing_script_via_stdin_and_returns_only_public_result(tmp_path, monkeypatch):
    script = tmp_path / "banner.py"
    script.write_text('import json,sys\nassert sys.argv[1:]==["dismiss","--payload-stdin"]\n'
                      'assert json.load(sys.stdin)=={"id":"alert:abc|1"}\n'
                      'print(json.dumps({"ok":True,"provider_body":"private"}))\n')
    monkeypatch.setattr(control, "BANNER_SCRIPT", script)
    assert asyncio.run(control.dismiss_communication("alert:abc|1")) == {"ok": True}


def test_dismiss_process_death_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "BANNER_SCRIPT", tmp_path / "missing.py")
    result = asyncio.run(control.dismiss_communication("alert:abc"))
    assert result == {"ok": False, "error": "dismiss_unavailable", "retryable": True}


def test_concurrent_acknowledgments_preserve_all_instances(tmp_path):
    path = tmp_path / "acks.json"
    acks = [{"reminder_id": f"r-{i}", "instance_key": "2026-09-04", "revision": 1, "outcome": "applied"} for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(lambda ack: control.record_plan_ack(ack, path=path), acks))
    assert len(json.loads(path.read_text())) == 12


def test_ack_already_covered_by_a_stronger_outcome_is_accepted(tmp_path):
    path = tmp_path / "acks.json"
    ack = {"reminder_id": "r", "instance_key": "2026-09-04", "revision": 2, "outcome": "applied"}
    assert control.record_plan_ack(ack, path=path)
    assert control.record_plan_ack({**ack, "outcome": "degraded"}, path=path)
    assert control.record_plan_ack({**ack, "revision": 1}, path=path)
    assert json.loads(path.read_text())["r|2026-09-04"] == ack
