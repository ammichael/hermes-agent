"""Purpose-based inference routing for scheduled jobs."""

from unittest.mock import MagicMock, patch

import pytest

from cron.jobs import _compute_provider_model_snapshots
from cron.scheduler import run_job
from hermes_cli.config import _validate_config_key
from hermes_cli.inference_roles import (
    InferenceRoleError,
    is_inference_role_reference,
    resolve_inference_role,
)


def _config(route="codex-luna"):
    return {
        "inference": {
            "roles": {
                "routine": route,
                "balanced": "codex-terra",
                "frontier": "codex-sol",
            },
            "routes": {
                "codex-luna": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "codex-terra": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-terra",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "codex-sol": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                },
                "grok": {
                    "provider": "xai-oauth",
                    "model": "grok-4.5",
                },
            },
        }
    }


def test_role_reference_detection_is_explicit_and_bounded():
    assert is_inference_role_reference("role:routine") is True
    assert is_inference_role_reference("gpt-5.6-luna") is False
    assert is_inference_role_reference("role:") is False
    assert is_inference_role_reference("role:../../routine") is False


def test_role_resolves_through_named_route():
    resolved = resolve_inference_role("role:routine", _config())
    assert resolved == {
        "role": "routine",
        "route": "codex-luna",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }


def test_role_jobs_do_not_capture_concrete_drift_snapshots():
    assert _compute_provider_model_snapshots(
        provider=None,
        model="role:routine",
        base_url=None,
        no_agent=False,
    ) == (None, None)


def test_inference_config_paths_are_first_class():
    assert _validate_config_key("inference.roles.routine") == (True, None)
    assert _validate_config_key("inference.routes.codex-luna.model") == (True, None)


def test_one_scalar_role_change_swaps_provider_and_model():
    resolved = resolve_inference_role("role:routine", _config(route="grok"))
    assert resolved["route"] == "grok"
    assert resolved["provider"] == "xai-oauth"
    assert resolved["model"] == "grok-4.5"
    assert resolved["base_url"] is None


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({}, "missing_inference_config"),
        ({"inference": {"roles": {}, "routes": {}}}, "unknown_role"),
        (
            {"inference": {"roles": {"routine": "missing"}, "routes": {}}},
            "unknown_route",
        ),
        (
            {
                "inference": {
                    "roles": {"routine": "bad"},
                    "routes": {"bad": {"provider": "openai-codex"}},
                }
            },
            "invalid_route",
        ),
    ],
)
def test_invalid_role_configuration_fails_closed(config, reason):
    with pytest.raises(InferenceRoleError) as exc:
        resolve_inference_role("role:routine", config)
    assert exc.value.reason == reason
    assert "gpt-5.6" not in str(exc.value)


def test_scheduler_resolves_role_fresh_on_each_run(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
model:\n  default: gpt-5.6-sol\ninference:\n  roles:\n    routine: codex-luna\n  routes:\n    codex-luna:\n      provider: openai-codex\n      model: gpt-5.6-luna\n      base_url: https://chatgpt.com/backend-api/codex\n    grok:\n      provider: xai-oauth\n      model: grok-4.5\n"""
    )
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    job = {
        "id": "role-job",
        "name": "role job",
        "prompt": "hi",
        "model": "role:routine",
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
    }
    fake_db = MagicMock()

    resolved_runtimes = {
        "openai-codex": {
            "api_key": "test-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        },
        "xai-oauth": {
            "api_key": "test-key",
            "base_url": "https://api.x.ai/v1",
            "provider": "xai-oauth",
            "api_mode": "responses",
        },
    }

    def _resolve_runtime_provider(*, requested=None, **_kwargs):
        return resolved_runtimes[requested]

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             side_effect=_resolve_runtime_provider,
         ) as runtime_resolver, \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, _, _, error = run_job(job)
        assert success is True
        assert error is None
        assert mock_agent_cls.call_args.kwargs["model"] == "gpt-5.6-luna"
        assert runtime_resolver.call_args.kwargs["requested"] == "openai-codex"

        config_path.write_text(config_path.read_text().replace(
            "routine: codex-luna", "routine: grok"
        ))
        success, _, _, error = run_job(job)
        assert success is True
        assert error is None
        assert mock_agent_cls.call_args.kwargs["model"] == "grok-4.5"
        assert runtime_resolver.call_args.kwargs["requested"] == "xai-oauth"
