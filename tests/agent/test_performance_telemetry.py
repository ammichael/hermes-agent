from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path

from agent.performance_telemetry import (
    TurnPerformanceTelemetry,
    summarize_tool_messages,
)


def _write_record(home: str, index: int) -> None:
    os.environ["HERMES_HOME"] = home
    telemetry = TurnPerformanceTelemetry(
        session_id=f"private-session-{index}",
        started_monotonic=100.0,
        started_at="2026-07-20T12:00:00+00:00",
    )
    telemetry.mark_prologue_complete(now_monotonic=100.1)
    telemetry.record_model_call(
        duration_ms=1200,
        ttft_ms=300,
        success=True,
        provider="openai-codex",
        model="gpt-5.6-sol",
        request_chars=4000,
    )
    telemetry.record_tool_batch(
        duration_ms=40,
        tool_count=2,
        original_chars=5000,
        context_chars=1200,
        persisted_count=1,
    )
    telemetry.record_compression(duration_ms=75)
    telemetry.finalize(
        api_call_count=1,
        exit_reason="text_response(stop)",
        completed=True,
        finalization_ms=20,
        now_monotonic=101.5,
    )


def test_turn_telemetry_is_content_free_and_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw_session_id = "session-raw-secret-value"

    telemetry = TurnPerformanceTelemetry(
        session_id=raw_session_id,
        started_monotonic=10.0,
        started_at="2026-07-20T12:00:00+00:00",
    )
    telemetry.mark_prologue_complete(now_monotonic=10.2)
    telemetry.record_model_call(
        duration_ms=900,
        ttft_ms=250,
        success=False,
        provider="openai-codex",
        model="gpt-5.6-sol",
        request_chars=1234,
    )
    telemetry.record_tool_batch(
        duration_ms=80,
        tool_count=3,
        original_chars=9000,
        context_chars=1500,
        persisted_count=2,
    )
    row = telemetry.finalize(
        api_call_count=1,
        exit_reason="provider_error",
        completed=False,
        finalization_ms=30,
        now_monotonic=11.5,
    )

    metrics_path = tmp_path / "metrics" / "turn-performance.jsonl"
    key_path = tmp_path / "metrics" / ".telemetry-key"
    persisted = json.loads(metrics_path.read_text().strip())
    encoded = json.dumps(persisted, sort_keys=True)

    assert persisted == row
    assert raw_session_id not in encoded
    assert "session_id" not in persisted
    assert len(persisted["session_ref"]) == 24
    assert persisted["turn_duration_ms"] == 1500
    assert persisted["prologue_ms"] == 200
    assert persisted["model_duration_ms"] == 900
    assert persisted["tool_work_ms"] == 80
    assert persisted["tool_original_chars"] == 9000
    assert persisted["tool_context_chars"] == 1500
    assert persisted["persisted_tool_results"] == 2
    assert persisted["model_calls"][0]["ttft_ms"] == 250
    assert persisted["api_call_count"] == 1
    assert persisted["completed"] is False

    forbidden_keys = {
        "prompt",
        "message",
        "arguments",
        "result",
        "path",
        "user_id",
        "chat_id",
        "session_id",
        "base_url",
    }
    assert forbidden_keys.isdisjoint(persisted)
    assert oct(metrics_path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(metrics_path.stat().st_mode & 0o777) == "0o600"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"


def test_summarize_tool_messages_recovers_original_spill_size():
    metrics = summarize_tool_messages([
        {"role": "assistant", "content": "ignored"},
        {"role": "tool", "content": "small"},
        {
            "role": "tool",
            "content": (
                "<persisted-output>\n"
                "This tool result was too large (123,456 characters, 120.6 KB).\n"
                "Preview only\n"
                "</persisted-output>"
            ),
        },
    ])

    assert metrics["tool_count"] == 2
    assert metrics["persisted_count"] == 1
    assert metrics["original_chars"] == 123_461
    assert metrics["context_chars"] > len("small")

    false_positive = summarize_tool_messages([
        {
            "role": "tool",
            "content": "This tool result was too large (999,999 characters, 1 MB).",
        }
    ])
    assert false_positive["persisted_count"] == 0
    assert false_positive["original_chars"] == false_positive["context_chars"]


def test_corrupt_key_is_recreated_and_model_path_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(mode=0o700)
    key_path = metrics_dir / ".telemetry-key"
    key_path.write_bytes(b"partial")
    key_path.chmod(0o600)

    telemetry = TurnPerformanceTelemetry(
        session_id="private-session",
        started_monotonic=1.0,
        started_at="2026-07-20T12:00:00+00:00",
    )
    telemetry.record_model_call(
        duration_ms=1,
        ttft_ms=1,
        success=True,
        provider="local",
        model="/Users/private/models/model.gguf",
        request_chars=1,
    )
    row = telemetry.finalize(
        api_call_count=1,
        exit_reason="text_response(stop)",
        completed=True,
        now_monotonic=1.1,
    )

    assert len(key_path.read_bytes()) == 32
    assert row["model_calls"][0]["model"] == "other"
    assert "/Users/" not in json.dumps(row)


def test_turn_telemetry_concurrent_writers_keep_valid_jsonl(tmp_path):
    ctx = mp.get_context("spawn")
    workers = [ctx.Process(target=_write_record, args=(str(tmp_path), index)) for index in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    metrics_path = Path(tmp_path) / "metrics" / "turn-performance.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text().splitlines() if line]
    assert len(rows) == 8
    assert len({row["session_ref"] for row in rows}) == 8
    assert all(row["schema_version"] == 1 for row in rows)
