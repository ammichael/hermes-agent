from __future__ import annotations

import importlib
import json


def test_agenticos_feedback_registry_and_event_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import gateway.agenticos_feedback as feedback

    feedback = importlib.reload(feedback)
    token = feedback.register_candidate(
        {
            "run_id": "run-1",
            "candidate_id": "1",
            "title": "Candidate title",
            "sources": ["source.md:1"],
        }
    )

    assert len(token) == 12
    assert feedback.parse_callback_data(f"ago:{token}:useful") == (token, "useful")

    result = feedback.record_feedback(
        token=token,
        action="useful",
        user_id="1000000000",
        user_name="Requester",
        chat_id="-1000000000000",
        thread_id="123",
        message_id="456",
        callback_data=f"ago:{token}:useful",
    )

    assert result.label == "👍 útil"
    assert result.candidate_title == "Candidate title"
    events = result.event_path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    event = json.loads(events[0])
    assert event["action"] == "useful"
    assert event["candidate"]["run_id"] == "run-1"
    assert event["safety"]["external_writes"] is False


def test_agenticos_feedback_rejects_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import gateway.agenticos_feedback as feedback

    feedback = importlib.reload(feedback)
    token = feedback.register_candidate(
        {"run_id": "run-1", "candidate_id": "1", "title": "Candidate title"}
    )

    try:
        feedback.record_feedback(token=token, action="delete_everything")
    except ValueError as exc:
        assert "unknown AgenticOS feedback action" in str(exc)
    else:
        raise AssertionError("unknown action should raise ValueError")
