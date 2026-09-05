"""A synthesized turn's row is typed when it is WRITTEN, not when it ends.

Synthetic user turns (the crash-recovery note, delegation completions) are
model-facing scaffolding that must read as a timeline event, never as a user
bubble. The type used to be stamped only after ``run_conversation`` returned,
which left two holes:

* the row sits untyped for the whole turn, so any surface reading the
  transcript mid-turn paints the raw ``[System note: …]`` text as if the user
  had typed it;
* a turn killed before it concludes never reaches the stamp at all — and the
  auto-continue turn is precisely the one that gets killed twice, so its note
  stays a user bubble forever.

``persist_user_display_kind`` moves the typing to turn start, where the
crash-resilience persist writes it in the same insert as the content.
"""

from __future__ import annotations

import shutil
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.turn_context import build_turn_context
from hermes_state import SessionDB
from run_agent import AIAgent

NOTE = "[System note: Your previous turn was interrupted mid-run …]\n\nkeep going"


@pytest.fixture()
def agent_db():
    tmp = tempfile.mkdtemp(prefix="synthetic_display_kind_")
    db = SessionDB(Path(tmp) / "state.db")
    sid = "sess-synthetic"
    db.create_session(session_id=sid, source="desktop", model="test-model")
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=sid,
    )
    agent._session_db_created = True
    agent._cached_system_prompt = "SYSTEM"
    agent._skip_mcp_refresh = True
    try:
        yield agent, db, sid
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message=NOTE,
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        return build_turn_context(**kwargs)


def test_row_is_typed_by_the_turn_start_persist(agent_db):
    agent, db, sid = agent_db

    _build(
        agent,
        persist_user_display_kind="auto_continue",
        persist_user_display_metadata={"attempt": 2},
    )

    row, = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    # Typed before the turn ran — a crash from here on still reads as an event.
    assert row["display_kind"] == "auto_continue"
    assert row["display_metadata"] == {"attempt": 2}
    # The model's copy is untouched: same role, same content.
    assert row["content"] == NOTE


def test_a_real_user_turn_stays_untyped(agent_db):
    agent, db, sid = agent_db

    _build(agent, user_message="keep going")

    row, = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    assert row.get("display_kind") is None


def test_image_presentation_survives_early_persist_without_typing_user(agent_db):
    from agent.codex_responses_adapter import _summarize_user_message_for_log
    from gateway.platforms.api_server import APIServerAdapter, _companion_image_metadata

    agent, db, sid = agent_db
    url = "data:image/png;base64,aW1hZ2U="
    content = [{"type": "text", "text": "read this"},
               {"type": "image_url", "image_url": {"url": url}}]
    _build(agent, user_message=content,
           summarize_user_message_for_log=_summarize_user_message_for_log,
           persist_user_display_metadata=_companion_image_metadata(content))

    row, = [r for r in db.get_messages(sid) if r["role"] == "user"]
    assert row.get("display_kind") is None
    assert row["content"] == "read this\n[screenshot]"
    assert APIServerAdapter._message_response(row)["content"] == f"read this\n![image]({url})"
    replay, = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    assert replay["content"] == row["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("source", ["companion_ios", "api_server"])
async def test_image_http_turn_reopens_durable_history(agent_db, stream, source):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from agent.codex_responses_adapter import _summarize_user_message_for_log
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    agent, db, _ = agent_db
    sid = "image-http-test"
    db.create_session(session_id=sid, source=source, model="test-model")
    agent.session_id = sid
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test"}))
    adapter._session_db = db
    adapter._create_agent = lambda **kwargs: agent
    url = "data:image/png;base64,aW1hZ2U="
    content = [{"type": "text", "text": "read this"},
               {"type": "image_url", "image_url": {"url": url}}]

    def run(**kwargs):
        _build(agent, **kwargs, summarize_user_message_for_log=_summarize_user_message_for_log)
        return {"final_response": "image received", "messages": [], "api_calls": 1}

    agent.run_conversation = run
    app = web.Application()
    path = "/api/sessions/{session_id}"
    app.router.add_post(path + "/chat", adapter._handle_session_chat)
    app.router.add_post(path + "/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_get(path + "/messages", adapter._handle_session_messages)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/sessions/{sid}/chat" + ("/stream" if stream else ""),
            json={"message": content}, headers={"Authorization": "Bearer test"},
        )
        assert response.status == 200, await response.text()
        result = await response.text()
        assert "image received" in result
        # A new DB connection rules out an in-memory transcript-only fix.
        reopened = SessionDB(db.db_path)
        adapter._session_db = reopened
        try:
            response = await client.get(f"/api/sessions/{sid}/messages",
                                        headers={"Authorization": "Bearer test"})
            assert response.status == 200
            body = await response.json()
            row, = [r for r in body["data"] if r["role"] == "user"]
            expected = f"read this\n![image]({url})" if source == "companion_ios" else "read this\n[screenshot]"
            assert row["content"] == expected
            assert "display_metadata" not in row
        finally:
            reopened.close()
