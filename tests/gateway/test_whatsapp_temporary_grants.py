import json
from datetime import datetime, timedelta, timezone

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.whatsapp_temporary_grants import find_temporary_interaction_grant


def _write_grants(path, grants):
    path.write_text(json.dumps({"schema_version": 2, "grants": grants}), encoding="utf-8")


def _grant(**overrides):
    now = datetime.now(timezone.utc)
    grant = {
        "id": "grant-1",
        "kind": "temporary_interaction",
        "enabled": True,
        "chat_jid": "5511999999999@s.whatsapp.net",
        "participant_jids": ["5511999999999@s.whatsapp.net", "123456789@lid"],
        "topic": "delivery follow-up",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "delivery_message_ids": ["ABC123"],
        "capabilities": ["text_reply"],
    }
    grant.update(overrides)
    return grant


def _source(**overrides):
    values = {
        "platform": Platform.WHATSAPP,
        "chat_id": "5511999999999@s.whatsapp.net",
        "chat_type": "dm",
        "user_id": "123456789@lid",
    }
    values.update(overrides)
    return SessionSource(**values)


def test_exact_active_delivered_text_grant_matches(tmp_path):
    state = tmp_path / "grants.json"
    _write_grants(state, [_grant()])

    match = find_temporary_interaction_grant(_source(), state_path=state)

    assert match is not None
    assert match["id"] == "grant-1"


def test_expired_failed_missing_or_wrong_scope_grants_fail_closed(tmp_path):
    state = tmp_path / "grants.json"
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    invalid = [
        _grant(id="expired", expires_at=expired),
        _grant(id="failed", delivery_message_ids=[]),
        _grant(id="media", capabilities=["text_reply", "media"]),
        _grant(id="wrong-chat", chat_jid="5511888888888@s.whatsapp.net"),
    ]
    _write_grants(state, invalid)

    assert find_temporary_interaction_grant(_source(), state_path=state) is None
    assert find_temporary_interaction_grant(
        _source(chat_type="group"), state_path=state
    ) is None
    assert find_temporary_interaction_grant(
        _source(user_id="999@lid"), state_path=state
    ) is None


def test_schema_v1_legacy_temp_allow_file_is_not_trusted(tmp_path):
    state = tmp_path / "grants.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "grants": [
                    {
                        "ids": ["123456789"],
                        "topic": "legacy",
                        "expires_at": (
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert find_temporary_interaction_grant(_source(), state_path=state) is None
