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


def test_real_run_job_does_not_downgrade_concurrent_gateway_approval_context(
    tmp_path, monkeypatch
):
    """Exercise the public scheduler and approval boundary concurrently.

    The historical implementation set ``HERMES_CRON_SESSION`` process-wide.
    While a cron agent was running, an unrelated WhatsApp turn therefore looked
    like cron and skipped the interactive gateway approval path.
    """
    from cron.scheduler import run_job
    import gateway.session_context as session_context
    from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars
    from tools.approval import _is_gateway_approval_context

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(session_context, "_session_context_engaged", False)
    cron_entered = threading.Event()
    release_cron = threading.Event()
    result = {}

    class BlockingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            from utils import env_var_enabled

            result["cron_inside"] = env_var_enabled("HERMES_CRON_SESSION")
            cron_entered.set()
            assert release_cron.wait(timeout=5)
            return {"final_response": "ok"}

    job = {
        "id": "concurrent-identity-job",
        "name": "concurrent identity",
        "prompt": "hello",
        "deliver": "local",
    }
    fake_db = MagicMock()
    gateway_tokens = set_session_vars(platform="whatsapp", chat_id="owner-chat")
    try:
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
             patch("run_agent.AIAgent", BlockingAgent):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_job, job)
                assert cron_entered.wait(timeout=5)
                result["interactive_gateway"] = _is_gateway_approval_context()
                release_cron.set()
                success, _output, _response, error = future.result(timeout=10)
    finally:
        release_cron.set()
        clear_session_vars(gateway_tokens)
        reset_session_vars()

    assert success is True
    assert error is None
    assert result == {"cron_inside": True, "interactive_gateway": True}
    assert os.getenv("HERMES_CRON_SESSION") is None


def test_run_job_exception_cleans_cron_identity(tmp_path, monkeypatch):
    from cron.scheduler import run_job
    from utils import env_var_enabled

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    fake_db = MagicMock()

    class FailingAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            assert env_var_enabled("HERMES_CRON_SESSION") is True
            raise RuntimeError("deterministic test failure")

    job = {
        "id": "failing-identity-job",
        "name": "failing identity",
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
         patch("run_agent.AIAgent", FailingAgent):
        success, _output, _response, error = run_job(job)

    assert success is False
    assert "deterministic test failure" in str(error)
    assert env_var_enabled("HERMES_CRON_SESSION") is False
    assert os.getenv("HERMES_CRON_SESSION") is None
