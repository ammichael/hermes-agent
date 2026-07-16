"""Cron must not promise detached completion delivery it cannot route."""

from unittest.mock import MagicMock, patch

from cron.scheduler import run_job
from gateway.session_context import (
    async_delivery_supported,
    get_session_env,
    reset_session_vars,
    set_session_vars,
)


def test_run_job_disables_async_delivery_and_clears_inherited_context(tmp_path):
    """A cron turn is bounded even when its worker inherits a gateway context."""
    seen = {}
    fake_agent = MagicMock()

    def fake_run_conversation(*_args, **_kwargs):
        seen["source"] = get_session_env("HERMES_SESSION_SOURCE")
        seen["platform"] = get_session_env("HERMES_SESSION_PLATFORM")
        seen["chat_id"] = get_session_env("HERMES_SESSION_CHAT_ID")
        seen["async_delivery"] = async_delivery_supported()
        return {"final_response": "ok"}

    fake_agent.run_conversation.side_effect = fake_run_conversation
    set_session_vars(
        platform="telegram",
        source="telegram",
        chat_id="parent-chat",
        session_key="agent:main:telegram:dm:parent-chat",
        async_delivery=True,
    )

    job = {
        "id": "job-1",
        "name": "async-delivery-boundary",
        "prompt": "respond ok",
        "model": "test-model",
    }

    try:
        with (
            patch("cron.scheduler._hermes_home", tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("hermes_cli.env_loader.load_hermes_dotenv"),
            patch("hermes_cli.env_loader.reset_secret_source_cache"),
            patch("hermes_state.SessionDB", return_value=MagicMock()),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            ),
            patch("run_agent.AIAgent", return_value=fake_agent),
        ):
            success, _output, final_response, error = run_job(job)

        assert success is True
        assert final_response == "ok"
        assert error is None
        assert seen == {
            "source": "cron",
            "platform": "",
            "chat_id": "",
            "async_delivery": False,
        }

        # The bounded cron decision is turn-scoped and must not poison the next
        # unbound CLI/unaware path after cleanup.
        assert get_session_env("HERMES_SESSION_SOURCE") == ""
        assert get_session_env("HERMES_SESSION_PLATFORM") == ""
        assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
        assert async_delivery_supported() is True
    finally:
        reset_session_vars()
