"""Cron identity must be task-local, never process-global."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch


def test_cron_identity_contextvar_isolated_between_threads(monkeypatch):
    from gateway.session_context import _VAR_MAP
    from utils import env_var_enabled

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    cron_var = _VAR_MAP["HERMES_CRON_SESSION"]
    barrier = threading.Barrier(2)
    seen = {}

    def cron_turn():
        token = cron_var.set("1")
        try:
            barrier.wait()
            seen["cron"] = env_var_enabled("HERMES_CRON_SESSION")
        finally:
            cron_var.reset(token)

    def interactive_turn():
        token = cron_var.set("")
        try:
            barrier.wait()
            seen["interactive"] = env_var_enabled("HERMES_CRON_SESSION")
        finally:
            cron_var.reset(token)

    first = threading.Thread(target=cron_turn)
    second = threading.Thread(target=interactive_turn)
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert seen == {"cron": True, "interactive": False}
    assert os.getenv("HERMES_CRON_SESSION") is None


def test_cron_identity_survives_500_concurrent_interleavings(monkeypatch):
    from gateway.session_context import _VAR_MAP
    from utils import env_var_enabled

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    cron_var = _VAR_MAP["HERMES_CRON_SESSION"]
    start = threading.Event()

    def observe(index: int) -> bool:
        expected = index % 2 == 0
        token = cron_var.set("1" if expected else "")
        try:
            start.wait(timeout=5)
            return env_var_enabled("HERMES_CRON_SESSION") is expected
        finally:
            cron_var.reset(token)

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(observe, index) for index in range(500)]
        start.set()
        assert all(future.result(timeout=10) for future in futures)

    assert os.getenv("HERMES_CRON_SESSION") is None


def test_cron_identity_is_bridged_only_to_cron_subprocess(monkeypatch):
    from gateway.session_context import _VAR_MAP
    from tools.environments.local import _inject_session_context_env

    monkeypatch.setenv("HERMES_CRON_SESSION", "stale-global")
    cron_var = _VAR_MAP["HERMES_CRON_SESSION"]

    cron_token = cron_var.set("1")
    try:
        cron_env = dict(os.environ)
        _inject_session_context_env(cron_env)
    finally:
        cron_var.reset(cron_token)

    interactive_token = cron_var.set("")
    try:
        interactive_env = dict(os.environ)
        _inject_session_context_env(interactive_env)
    finally:
        cron_var.reset(interactive_token)

    assert cron_env["HERMES_CRON_SESSION"] == "1"
    assert interactive_env["HERMES_CRON_SESSION"] == ""


def test_run_job_binds_cron_identity_without_mutating_process_env(tmp_path, monkeypatch):
    from cron.scheduler import run_job

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    fake_db = MagicMock()
    seen = {}

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            from utils import env_var_enabled

            seen["inside"] = env_var_enabled("HERMES_CRON_SESSION")
            seen["process_env"] = os.getenv("HERMES_CRON_SESSION")
            return {"final_response": "ok"}

    job = {
        "id": "identity-job",
        "name": "identity",
        "prompt": "hello",
        "deliver": "local",
    }

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "provider": "openrouter",
                 "api_key": "x",
                 "base_url": "https://example.invalid",
                 "api_mode": "chat_completions",
             },
         ), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("run_agent.AIAgent", FakeAgent):
        success, _output, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert final_response == "ok"
    assert seen == {"inside": True, "process_env": None}
    assert os.getenv("HERMES_CRON_SESSION") is None
