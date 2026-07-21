"""Adapter regressions for OpenAI hosted tool search."""

from types import SimpleNamespace

from agent.codex_responses_adapter import (
    _normalize_codex_response,
    _preflight_codex_api_kwargs,
)


def _hosted_tools():
    return [
        {
            "type": "namespace",
            "name": "hermes_file",
            "description": "Hermes file capabilities: read_file.",
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a file.",
                    "strict": False,
                    "defer_loading": True,
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
        },
        {"type": "tool_search"},
    ]


def test_preflight_preserves_namespace_deferred_schema_and_tool_search():
    kwargs = {
        "model": "gpt-5.6-sol",
        "instructions": "Use tools.",
        "input": [{"role": "user", "content": "read"}],
        "tools": _hosted_tools(),
        "store": False,
    }

    normalized = _preflight_codex_api_kwargs(kwargs)

    assert normalized["tools"] == _hosted_tools()


def test_normalizer_ignores_hosted_search_progress_and_keeps_function_call():
    response = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text="",
        output=[
            SimpleNamespace(type="tool_search_call", status="in_progress"),
            SimpleNamespace(type="tool_search_output", status="completed"),
            SimpleNamespace(
                type="function_call",
                status="completed",
                name="read_file",
                namespace="hermes_file",
                arguments='{"path":"/tmp/demo"}',
                call_id="call_hosted_1",
                id="fc_hosted_1",
            ),
        ],
    )

    message, finish_reason = _normalize_codex_response(response)

    assert finish_reason == "tool_calls"
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].function.name == "read_file"
    assert message.tool_calls[0].call_id == "call_hosted_1"
