"""Runtime tests for hosted tool-search activation and compatibility fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.codex_runtime import run_codex_stream
from tools.hosted_tool_search import HostedToolSearchConfig


def _direct_tool(name: str = "demo_ping") -> dict:
    return {
        "type": "function",
        "name": name,
        "description": "Return ping.",
        "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }


class _ToolShape400(Exception):
    status_code = 400


class _Responses:
    def __init__(self, *, reject_hosted: bool = False):
        self.calls = []
        self.reject_hosted = reject_hosted

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_hosted and any(
            tool.get("type") == "tool_search" for tool in kwargs.get("tools", [])
        ):
            raise _ToolShape400("Invalid Value: 'tools'. tool_search unsupported")
        return SimpleNamespace(
            output=[SimpleNamespace(type="message")],
            status="completed",
        )


class _Client:
    def __init__(self, responses):
        self.responses = responses


class _Agent:
    provider = "openai-codex"
    model = "gpt-5.6-sol"
    base_url = "https://chatgpt.com/backend-api/codex"
    api_mode = "codex_responses"
    _interrupt_requested = False

    def __init__(self):
        self._codex_streamed_text_parts = []

    def _fire_stream_delta(self, _text):
        return None

    def _fire_reasoning_delta(self, _text):
        return None

    def _fire_streamed_codex_commentary(self, _text):
        return None

    def _touch_activity(self, _label):
        return None

    def _client_log_context(self):
        return "test"


def _kwargs():
    return {
        "model": "gpt-5.6-sol",
        "instructions": "Be exact.",
        "input": [{"role": "user", "content": "ping"}],
        "tools": [_direct_tool()],
        "store": False,
    }


def test_runtime_sends_hosted_namespace_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "tools.hosted_tool_search.load_config",
        lambda: HostedToolSearchConfig(enabled=True),
    )
    responses = _Responses()

    result = run_codex_stream(_Agent(), _kwargs(), client=_Client(responses))

    assert result is not None
    assert result.status == "completed"
    assert len(responses.calls) == 1
    sent_tools = responses.calls[0]["tools"]
    assert any(tool.get("type") == "namespace" for tool in sent_tools)
    assert any(tool.get("type") == "tool_search" for tool in sent_tools)
    assert not any(tool.get("type") == "function" for tool in sent_tools)


def test_runtime_retries_once_with_direct_schemas_on_tool_shape_400(monkeypatch):
    monkeypatch.setattr(
        "tools.hosted_tool_search.load_config",
        lambda: HostedToolSearchConfig(enabled=True),
    )
    responses = _Responses(reject_hosted=True)

    result = run_codex_stream(_Agent(), _kwargs(), client=_Client(responses))

    assert result is not None
    assert result.status == "completed"
    assert len(responses.calls) == 2
    assert any(
        tool.get("type") == "tool_search" for tool in responses.calls[0]["tools"]
    )
    assert responses.calls[1]["tools"] == [_direct_tool()]


def test_runtime_does_not_fallback_for_unrelated_400(monkeypatch):
    monkeypatch.setattr(
        "tools.hosted_tool_search.load_config",
        lambda: HostedToolSearchConfig(enabled=True),
    )

    class _UnrelatedResponses(_Responses):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            exc = _ToolShape400("invalid_encrypted_content")
            raise exc

    responses = _UnrelatedResponses()
    with pytest.raises(_ToolShape400):
        run_codex_stream(_Agent(), _kwargs(), client=_Client(responses))
    assert len(responses.calls) == 1
