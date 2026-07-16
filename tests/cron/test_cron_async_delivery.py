"""Cron must not promise detached completion delivery it cannot route."""

from cron.scheduler import _cron_execution_scope
from gateway.session_context import (
    async_delivery_supported,
    get_session_env,
    restore_session_vars,
    set_session_vars,
)


def test_cron_scope_disables_async_delivery_and_restores_parent_context():
    parent_tokens = set_session_vars(
        platform="telegram",
        source="telegram",
        chat_id="parent-chat",
        session_key="agent:main:telegram:dm:parent-chat",
        async_delivery=True,
    )
    try:
        with _cron_execution_scope({"id": "job-1"}, "job-1"):
            assert get_session_env("HERMES_SESSION_SOURCE") == "cron"
            assert get_session_env("HERMES_SESSION_PLATFORM") == ""
            assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
            assert async_delivery_supported() is False

        assert get_session_env("HERMES_SESSION_SOURCE") == "telegram"
        assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
        assert get_session_env("HERMES_SESSION_CHAT_ID") == "parent-chat"
        assert async_delivery_supported() is True
    finally:
        restore_session_vars(parent_tokens)