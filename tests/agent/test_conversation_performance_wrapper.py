from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import conversation_loop


def _rows(tmp_path):
    path = tmp_path / "metrics" / "turn-performance.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_wrapper_finalizes_early_return(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = SimpleNamespace(session_id="raw-session", _api_call_count=2)
    monkeypatch.setattr(
        conversation_loop,
        "_run_conversation_impl",
        lambda *args, **kwargs: {
            "completed": False,
            "api_calls": 2,
            "final_response": "not persisted by telemetry",
        },
    )

    result = conversation_loop.run_conversation(agent, "private user message")

    assert result["api_calls"] == 2
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "early_return"
    assert rows[0]["completed"] is False
    encoded = json.dumps(rows[0])
    assert "raw-session" not in encoded
    assert "private user message" not in encoded
    assert "not persisted by telemetry" not in encoded
    assert not hasattr(agent, "_turn_performance_telemetry")


def test_wrapper_finalizes_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = SimpleNamespace(session_id="raw-session", _api_call_count=3)

    def _raise(*args, **kwargs):
        raise RuntimeError("private exception detail")

    monkeypatch.setattr(conversation_loop, "_run_conversation_impl", _raise)

    with pytest.raises(RuntimeError, match="private exception detail"):
        conversation_loop.run_conversation(agent, "private user message")

    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "exception"
    assert rows[0]["api_call_count"] == 3
    assert "private exception detail" not in json.dumps(rows[0])
    assert not hasattr(agent, "_turn_performance_telemetry")
