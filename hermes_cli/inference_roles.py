"""Purpose-based inference routing.

Scheduled jobs can store ``model: role:<name>`` instead of pinning a concrete
provider/model pair. The role resolves through two config maps on every run::

    inference:
      roles:
        routine: codex-luna
      routes:
        codex-luna:
          provider: openai-codex
          model: gpt-5.6-luna
          base_url: https://chatgpt.com/backend-api/codex

Changing ``inference.roles.routine`` is then enough to move every routine job
to another predeclared route without rewriting the job store.
"""

from __future__ import annotations

import re
from typing import Any, Optional, TypedDict

_ROLE_PREFIX = "role:"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ROUTE_KEYS = frozenset({"provider", "model", "base_url"})


class InferenceRoleError(ValueError):
    """Sanitized, fail-closed inference-role configuration error."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Inference role resolution failed: {reason}")


class ResolvedInferenceRole(TypedDict):
    role: str
    route: str
    provider: str
    model: str
    base_url: Optional[str]


def is_inference_role_reference(value: Any) -> bool:
    """Return whether *value* is a syntactically valid ``role:<name>`` reference."""
    if not isinstance(value, str) or not value.startswith(_ROLE_PREFIX):
        return False
    return bool(_NAME_RE.fullmatch(value[len(_ROLE_PREFIX) :]))


def validate_inference_role_job_fields(
    model: Any,
    provider: Any,
    base_url: Any,
) -> None:
    """Reject ambiguous jobs that mix a role with concrete routing fields."""
    if not isinstance(model, str) or not model.startswith(_ROLE_PREFIX):
        return
    if not is_inference_role_reference(model):
        raise InferenceRoleError("invalid_reference")
    if isinstance(provider, str) and provider.strip():
        raise InferenceRoleError("role_with_provider_pin")
    if isinstance(base_url, str) and base_url.strip():
        raise InferenceRoleError("role_with_base_url_pin")


def resolve_inference_role(
    reference: Any,
    config: Any,
) -> Optional[ResolvedInferenceRole]:
    """Resolve ``role:<name>`` through ``inference.roles`` and ``routes``.

    Non-role model strings return ``None``. Role-shaped strings fail closed on
    malformed or incomplete configuration. Returned values are normalized and
    contain no credentials.
    """
    if not isinstance(reference, str) or not reference.startswith(_ROLE_PREFIX):
        return None
    if not is_inference_role_reference(reference):
        raise InferenceRoleError("invalid_reference")
    if not isinstance(config, dict):
        raise InferenceRoleError("missing_inference_config")

    inference = config.get("inference")
    if not isinstance(inference, dict):
        raise InferenceRoleError("missing_inference_config")
    roles = inference.get("roles")
    routes = inference.get("routes")
    if not isinstance(roles, dict) or not isinstance(routes, dict):
        raise InferenceRoleError("missing_inference_config")

    role = reference[len(_ROLE_PREFIX) :]
    route_name = roles.get(role)
    if not isinstance(route_name, str) or not _NAME_RE.fullmatch(route_name):
        raise InferenceRoleError("unknown_role")

    route = routes.get(route_name)
    if not isinstance(route, dict):
        raise InferenceRoleError("unknown_route")
    if set(route) - _ROUTE_KEYS:
        raise InferenceRoleError("invalid_route")

    provider = route.get("provider")
    model = route.get("model")
    base_url = route.get("base_url")
    if not isinstance(provider, str) or not provider.strip():
        raise InferenceRoleError("invalid_route")
    if not isinstance(model, str) or not model.strip():
        raise InferenceRoleError("invalid_route")
    if base_url is not None and (
        not isinstance(base_url, str) or not base_url.strip()
    ):
        raise InferenceRoleError("invalid_route")

    return {
        "role": role,
        "route": route_name,
        "provider": provider.strip(),
        "model": model.strip(),
        "base_url": base_url.strip().rstrip("/") if base_url else None,
    }
