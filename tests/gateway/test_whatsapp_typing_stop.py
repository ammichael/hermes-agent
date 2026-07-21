"""Regression tests for clearing WhatsApp typing presence after a turn."""

from unittest.mock import AsyncMock, MagicMock

import pytest


class _AsyncCM:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter():
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter._running = True
    adapter._bridge_port = 19876
    adapter._check_managed_bridge_exit = AsyncMock(return_value=None)

    response = MagicMock()
    session = MagicMock()
    session.post = MagicMock(return_value=_AsyncCM(response))
    adapter._http_session = session
    return adapter, session


@pytest.mark.asyncio
async def test_typing_presence_is_started_and_explicitly_stopped():
    adapter, session = _make_adapter()
    chat_id = "120363426743489720@g.us"

    await adapter.send_typing(chat_id)
    await adapter.stop_typing(chat_id)

    assert session.post.call_count == 2
    start_call, stop_call = session.post.call_args_list
    assert start_call.kwargs["json"] == {
        "chatId": chat_id,
        "state": "composing",
    }
    assert stop_call.kwargs["json"] == {
        "chatId": chat_id,
        "state": "paused",
    }
