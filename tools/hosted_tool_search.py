"""OpenAI Responses hosted tool search for Hermes core tool schemas.

This is deliberately separate from :mod:`tools.tool_search`.  The older module
implements client-side progressive disclosure for MCP/plugin tools and must
never hide Hermes core tools because resolving a call costs extra agent turns.
OpenAI's hosted tool search is different: the provider expands a deferred
schema and emits the function call inside the *same* response.  That lets us
keep every capability while removing large function schemas from the model's
cold prompt.

The feature is opt-in and limited to the live-tested ChatGPT Codex Responses
surface.  Unsupported providers receive the original tool list byte-for-byte.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("tools.hosted_tool_search")

_HOSTED_NAMESPACE_PREFIX = "hermes_"
_HOSTED_SEARCH_TOOL = {"type": "tool_search"}
_CLIENT_BRIDGE_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
_DEFAULT_MAX_TOOLS_PER_NAMESPACE = 8
_MAX_TOOLS_PER_NAMESPACE = 8  # OpenAI recommends fewer than 10.


@dataclass(frozen=True)
class HostedToolSearchConfig:
    """Validated user configuration for hosted tool search."""

    enabled: bool = False
    max_tools_per_namespace: int = _DEFAULT_MAX_TOOLS_PER_NAMESPACE

    @classmethod
    def from_raw(cls, raw: Any) -> "HostedToolSearchConfig":
        if raw is True:
            return cls(enabled=True)
        if raw is False or raw is None:
            return cls(enabled=False)
        if not isinstance(raw, dict):
            return cls(enabled=False)

        enabled_raw = raw.get("enabled", False)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() in {"true", "1", "yes", "on"}
        else:
            enabled = enabled_raw is True

        try:
            max_tools = int(raw.get("max_tools_per_namespace", _DEFAULT_MAX_TOOLS_PER_NAMESPACE))
        except (TypeError, ValueError):
            max_tools = _DEFAULT_MAX_TOOLS_PER_NAMESPACE
        max_tools = max(1, min(_MAX_TOOLS_PER_NAMESPACE, max_tools))
        return cls(enabled=enabled, max_tools_per_namespace=max_tools)


def load_config() -> HostedToolSearchConfig:
    """Load ``tools.hosted_tool_search`` from the active Hermes config."""

    try:
        from hermes_cli.config import load_config as _load

        cfg = _load() or {}
        raw_tools_cfg = cfg.get("tools")
        tools_cfg: Dict[str, Any] = raw_tools_cfg if isinstance(raw_tools_cfg, dict) else {}
        return HostedToolSearchConfig.from_raw(tools_cfg.get("hosted_tool_search"))
    except Exception as exc:  # pragma: no cover - config failure must not break turns
        logger.debug("Failed to load hosted-tool-search config: %s", exc)
        return HostedToolSearchConfig()


def _model_supports_hosted_tool_search(model: str) -> bool:
    """Return whether a model is on the explicit live-tested version floor."""

    value = str(model or "").strip().lower()
    match = re.search(r"(?:^|/)gpt-(\d+)\.(\d+)", value)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return major > 5 or (major == 5 and minor >= 4)


def is_supported_runtime(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
) -> bool:
    """Gate hosted search to the exact Responses runtime proven live."""

    if str(api_mode or "").strip().lower() != "codex_responses":
        return False
    if str(provider or "").strip().lower() != "openai-codex":
        return False
    if not _model_supports_hosted_tool_search(model):
        return False

    parsed = urlparse(str(base_url or "").strip())
    return (
        (parsed.hostname or "").lower() == "chatgpt.com"
        and "/backend-api/codex" in (parsed.path or "").lower()
    )


def _toolset_for_name(name: str) -> str:
    """Resolve a deterministic namespace bucket for one registered function."""

    # fact-store tools predate registry toolset metadata but belong with memory.
    if name in {"fact_store", "fact_feedback"}:
        return "memory"
    try:
        from tools.registry import registry

        entry = registry.get_entry(name)
        toolset = getattr(entry, "toolset", "") if entry is not None else ""
        if isinstance(toolset, str) and toolset.strip():
            return toolset.strip()
    except Exception:
        pass
    return "other"


def _safe_namespace_name(toolset: str, chunk_number: int, chunk_count: int) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(toolset or "other")).strip("_").lower()
    base = base or "other"
    suffix = f"_{chunk_number}" if chunk_count > 1 else ""
    # Leave room for suffix and keep the provider's 64-char identifier ceiling.
    return f"{_HOSTED_NAMESPACE_PREFIX}{base}"[: 64 - len(suffix)] + suffix


def _namespace_description(toolset: str, functions: Iterable[Dict[str, Any]]) -> str:
    names = [str(item.get("name") or "") for item in functions]
    label = str(toolset or "other").replace("_", " ").replace("-", " ")
    return f"Hermes {label} capabilities: {', '.join(name for name in names if name)}."


@dataclass(frozen=True)
class HostedToolSearchAssembly:
    activated: bool
    tools: List[Dict[str, Any]]
    direct_tool_count: int
    namespace_count: int
    direct_schema_bytes: int
    visible_schema_bytes: int
    estimated_saved_tokens: int
    reason: str = ""


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def assemble_hosted_tools(
    response_tools: Optional[List[Dict[str, Any]]],
    *,
    config: HostedToolSearchConfig,
) -> HostedToolSearchAssembly:
    """Wrap Responses function schemas in deterministic deferred namespaces.

    The input list is never mutated.  Every valid function name appears exactly
    once in the returned namespaces and every original schema/parameter is
    preserved, with only ``defer_loading=true`` added.
    """

    original = list(response_tools or [])
    direct_bytes = _json_bytes(original)
    if not config.enabled or not original:
        return HostedToolSearchAssembly(
            False, original, len(original), 0, direct_bytes, direct_bytes, 0,
            "disabled_or_empty",
        )

    function_tools: List[Dict[str, Any]] = []
    passthrough: List[Dict[str, Any]] = []
    names: List[str] = []
    for raw_tool in original:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            passthrough.append(raw_tool)
            continue
        name = raw_tool.get("name")
        if not isinstance(name, str) or not name.strip():
            passthrough.append(raw_tool)
            continue
        name = name.strip()
        names.append(name)
        function_tools.append(dict(raw_tool, name=name))

    # Never stack native hosted search on top of Hermes's client bridge.  That
    # bridge has already removed underlying schemas, so wrapping it would save
    # little and could make capability discovery ambiguous.
    if _CLIENT_BRIDGE_NAMES.intersection(names):
        return HostedToolSearchAssembly(
            False, original, len(function_tools), 0, direct_bytes, direct_bytes, 0,
            "client_tool_search_bridge_present",
        )
    if not function_tools:
        return HostedToolSearchAssembly(
            False, original, 0, 0, direct_bytes, direct_bytes, 0,
            "no_function_tools",
        )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tool in sorted(function_tools, key=lambda item: str(item.get("name") or "")):
        grouped.setdefault(_toolset_for_name(str(tool["name"])), []).append(tool)

    namespaces: List[Dict[str, Any]] = []
    max_per_namespace = config.max_tools_per_namespace
    for toolset in sorted(grouped):
        group = grouped[toolset]
        chunk_count = math.ceil(len(group) / max_per_namespace)
        for offset in range(0, len(group), max_per_namespace):
            chunk = group[offset : offset + max_per_namespace]
            chunk_number = offset // max_per_namespace + 1
            namespaces.append(
                {
                    "type": "namespace",
                    "name": _safe_namespace_name(toolset, chunk_number, chunk_count),
                    "description": _namespace_description(toolset, chunk),
                    "tools": [dict(tool, defer_loading=True) for tool in chunk],
                }
            )

    hosted_tools = sorted(passthrough, key=lambda item: json.dumps(item, sort_keys=True))
    hosted_tools.extend(namespaces)
    hosted_tools.append(dict(_HOSTED_SEARCH_TOOL))

    # Deferred nested schemas are not model-visible until selected.  Estimate
    # the cold-prompt footprint from namespace declarations + passthrough only.
    visible_projection: List[Dict[str, Any]] = []
    visible_projection.extend(passthrough)
    visible_projection.extend(
        {key: ns[key] for key in ("type", "name", "description")}
        for ns in namespaces
    )
    visible_projection.append(dict(_HOSTED_SEARCH_TOOL))
    visible_bytes = _json_bytes(visible_projection)
    saved_tokens = max(0, math.ceil((direct_bytes - visible_bytes) / 4))

    return HostedToolSearchAssembly(
        True,
        hosted_tools,
        len(function_tools),
        len(namespaces),
        direct_bytes,
        visible_bytes,
        saved_tokens,
    )


def is_compatibility_error(exc: BaseException) -> bool:
    """Identify provider 400s that warrant one direct-schema retry."""

    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    message = str(exc).lower()
    markers = (
        "tool_search",
        "defer_loading",
        "namespace",
        "invalid value: 'tools'",
        'invalid value: "tools"',
        "unsupported tool",
        "unsupported parameter: tools",
        "invalid tools",
    )
    return any(marker in message for marker in markers)


__all__ = [
    "HostedToolSearchAssembly",
    "HostedToolSearchConfig",
    "assemble_hosted_tools",
    "is_compatibility_error",
    "is_supported_runtime",
    "load_config",
]
