"""Tests for OpenAI Responses hosted tool search."""

from __future__ import annotations

import copy

import pytest

from tools.hosted_tool_search import (
    HostedToolSearchConfig,
    assemble_hosted_tools,
    is_compatibility_error,
    is_supported_runtime,
)


def _tool(name: str, description: str = "demo") -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    }


def _nested_functions(tools: list[dict]) -> list[dict]:
    return [
        function
        for namespace in tools
        if namespace.get("type") == "namespace"
        for function in namespace.get("tools", [])
    ]


class TestHostedToolSearchConfig:
    def test_default_is_off(self):
        assert HostedToolSearchConfig.from_raw(None).enabled is False

    def test_bool_and_mapping_enable(self):
        assert HostedToolSearchConfig.from_raw(True).enabled is True
        assert HostedToolSearchConfig.from_raw({"enabled": "on"}).enabled is True

    def test_namespace_size_is_clamped_below_ten(self):
        assert HostedToolSearchConfig.from_raw(
            {"enabled": True, "max_tools_per_namespace": 999}
        ).max_tools_per_namespace == 8
        assert HostedToolSearchConfig.from_raw(
            {"enabled": True, "max_tools_per_namespace": 0}
        ).max_tools_per_namespace == 1


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "api_mode", "expected"),
    [
        (
            "openai-codex",
            "gpt-5.6-sol",
            "https://chatgpt.com/backend-api/codex",
            "codex_responses",
            True,
        ),
        (
            "openai-codex",
            "gpt-5.4",
            "https://chatgpt.com/backend-api/codex",
            "codex_responses",
            True,
        ),
        (
            "openai-codex",
            "gpt-5.3",
            "https://chatgpt.com/backend-api/codex",
            "codex_responses",
            False,
        ),
        (
            "openai",
            "gpt-5.6-sol",
            "https://api.openai.com/v1",
            "codex_responses",
            False,
        ),
        (
            "openai-codex",
            "gpt-5.6-sol",
            "https://chatgpt.com/backend-api/codex",
            "chat_completions",
            False,
        ),
    ],
)
def test_supported_runtime_is_explicitly_allowlisted(
    provider, model, base_url, api_mode, expected
):
    assert is_supported_runtime(
        provider=provider,
        model=model,
        base_url=base_url,
        api_mode=api_mode,
    ) is expected


def test_assembly_preserves_every_schema_and_does_not_mutate_input():
    direct = [
        _tool("read_file"),
        _tool("write_file"),
        _tool("web_search"),
        _tool("fact_store"),
    ]
    before = copy.deepcopy(direct)

    result = assemble_hosted_tools(
        direct,
        config=HostedToolSearchConfig(enabled=True, max_tools_per_namespace=2),
    )

    assert result.activated is True
    assert direct == before
    nested = _nested_functions(result.tools)
    assert {tool["name"] for tool in nested} == {tool["name"] for tool in direct}
    for tool in nested:
        original = next(item for item in direct if item["name"] == tool["name"])
        assert {key: value for key, value in tool.items() if key != "defer_loading"} == original
        assert tool["defer_loading"] is True
    assert result.tools[-1] == {"type": "tool_search"}
    assert all(
        namespace["name"].startswith("hermes_")
        for namespace in result.tools
        if namespace.get("type") == "namespace"
    )
    assert all(
        len(namespace["tools"]) <= 2
        for namespace in result.tools
        if namespace.get("type") == "namespace"
    )


def test_assembly_is_deterministic_across_input_order():
    tools = [_tool("web_extract"), _tool("read_file"), _tool("terminal")]
    cfg = HostedToolSearchConfig(enabled=True)
    forward = assemble_hosted_tools(tools, config=cfg)
    reverse = assemble_hosted_tools(list(reversed(tools)), config=cfg)
    assert forward.tools == reverse.tools


def test_client_bridge_disables_hosted_wrapping_without_dropping_tools():
    direct = [_tool("tool_search"), _tool("tool_describe"), _tool("tool_call")]
    result = assemble_hosted_tools(
        direct,
        config=HostedToolSearchConfig(enabled=True),
    )
    assert result.activated is False
    assert result.reason == "client_tool_search_bridge_present"
    assert result.tools == direct


def test_disabled_is_byte_for_byte_passthrough():
    direct = [_tool("terminal")]
    result = assemble_hosted_tools(
        direct,
        config=HostedToolSearchConfig(enabled=False),
    )
    assert result.activated is False
    assert result.tools == direct


class _FakeProviderError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_compatibility_error_is_narrow_to_tool_shape_400s():
    assert is_compatibility_error(
        _FakeProviderError(400, "Invalid Value: 'tools'. namespace unsupported")
    )
    assert not is_compatibility_error(
        _FakeProviderError(400, "invalid_encrypted_content")
    )
    assert not is_compatibility_error(
        _FakeProviderError(500, "tool_search unavailable")
    )
