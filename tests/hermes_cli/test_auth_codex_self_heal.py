"""Regression tests for Codex refresh and credential-source precedence.

Hermes keeps its OWN copy of the Codex OAuth token (per profile + top-level),
separate from the Codex CLI's ``~/.codex/auth.json``. Once the Hermes provider is
present, refresh failures must never consult or import the CLI store. A rejected
singleton refresh may fall back to a usable Hermes pool credential or its quota
state; otherwise the original refresh error is preserved.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import AuthError, _refresh_codex_auth_tokens, resolve_codex_runtime_credentials

STALE = {"access_token": "stale-access", "refresh_token": "stale-refresh"}


def _write_codex_pool(hermes_home, entries):
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {"openai-codex": entries},
    }))


def test_pool_quota_without_reset_is_not_returned_as_token(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _write_codex_pool(hermes_home, [{
        "access_token": "quota-access",
        "last_status": "exhausted",
        "last_error_code": 429,
    }])
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as exc_info:
        resolve_codex_runtime_credentials()

    assert exc_info.value.code == auth.CODEX_RATE_LIMITED_CODE
    assert exc_info.value.relogin_required is False


def test_pool_quota_with_future_string_reset_is_not_returned_as_token(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    future_reset = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _write_codex_pool(hermes_home, [{
        "access_token": "quota-access",
        "last_status": "exhausted",
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": future_reset,
    }])
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as exc_info:
        resolve_codex_runtime_credentials()

    assert exc_info.value.code == auth.CODEX_RATE_LIMITED_CODE
    assert exc_info.value.relogin_required is False


def test_pool_entry_becomes_eligible_after_parseable_reset_expires(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    expired_reset_ms = str(int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp() * 1000))
    _write_codex_pool(hermes_home, [{
        "access_token": "recovered-access",
        "last_status": "exhausted",
        "last_error_message": "quota exhausted",
        "last_error_reset_at": expired_reset_ms,
    }])
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "recovered-access"
    assert resolved["source"] == "credential_pool"


def test_pool_quota_entry_falls_back_to_another_healthy_account(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _write_codex_pool(hermes_home, [
        {
            "access_token": "quota-access",
            "last_status": "exhausted",
            "last_error_code": 429,
        },
        {"access_token": "healthy-access", "last_status": "ok"},
    ])
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "healthy-access"
    assert resolved["source"] == "credential_pool"


def test_rejected_present_provider_never_imports_codex_cli(monkeypatch):
    """A rejected refresh for a present provider propagates without CLI I/O."""
    saved = {}
    import_calls = {"n": 0}

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "fresh-access", "refresh_token": "fresh-refresh"}

    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    with pytest.raises(AuthError) as exc_info:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert exc_info.value.code == "invalid_grant"
    assert import_calls["n"] == 0
    assert saved == {}


def test_rejected_refresh_falls_back_to_usable_pool(tmp_path, monkeypatch):
    """A rejected singleton refresh falls back to a usable Hermes pool entry."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": dict(STALE),
                "auth_mode": "chatgpt",
            },
        },
        "credential_pool": {
            "openai-codex": [{
                "source": "manual:device_code",
                "access_token": "pool-access",
                "refresh_token": "pool-refresh",
            }],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import_calls = {"n": 0}

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "cli-access", "refresh_token": "cli-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)

    resolved = resolve_codex_runtime_credentials(force_refresh=True)

    assert resolved["api_key"] == "pool-access"
    assert resolved["source"] == "credential_pool"
    assert import_calls["n"] == 0


def test_rejected_refresh_falls_back_to_pool_quota(tmp_path, monkeypatch):
    """A rejected singleton refresh surfaces authoritative Hermes pool quota."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": dict(STALE),
                "auth_mode": "chatgpt",
            },
        },
        "credential_pool": {
            "openai-codex": [{
                "access_token": "quota-access",
                "last_status": "exhausted",
                "last_error_code": 429,
                "last_error_reason": "usage_limit_reached",
                "last_error_reset_at": 9999999999,
            }],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import_calls = {"n": 0}

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "cli-access", "refresh_token": "cli-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)

    with pytest.raises(AuthError) as exc_info:
        resolve_codex_runtime_credentials(force_refresh=True)

    assert exc_info.value.code == auth.CODEX_RATE_LIMITED_CODE
    assert exc_info.value.relogin_required is False
    assert import_calls["n"] == 0


def test_does_not_self_heal_on_rate_limit(monkeypatch):
    """429 quota keeps relogin_required=False — token still valid, must NOT reimport."""
    import_calls = {"n": 0}

    def _rate_limited(*_a, **_k):
        raise AuthError(
            "quota exhausted",
            provider="openai-codex",
            code="codex_rate_limited",
            relogin_required=False,
        )

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "should-not-be-used"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rate_limited)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda *a, **k: None)

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "codex_rate_limited"
    assert import_calls["n"] == 0  # never touched ~/.codex on a transient failure


def test_relogin_required_refresh_propagates_unchanged(monkeypatch):
    """Without a resolver-level pool fallback, preserve the refresh error."""
    import_calls = {"n": 0}

    def _reused(*_a, **_k):
        raise AuthError(
            "refresh token reused",
            provider="openai-codex",
            code="refresh_token_reused",
            relogin_required=True,
        )

    def _import_spy():
        import_calls["n"] += 1
        return None

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _reused)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda *a, **k: None)

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "refresh_token_reused"
    assert import_calls["n"] == 0


def test_happy_path_unchanged(monkeypatch):
    """Normal refresh succeeds → rotated tokens persisted, ~/.codex never consulted."""
    saved = {}
    import_calls = {"n": 0}

    def _import_spy():
        import_calls["n"] += 1
        return None

    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *a, **k: {"access_token": "rotated", "refresh_token": "rotated-r"},
    )
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    out = _refresh_codex_auth_tokens({"access_token": "a", "refresh_token": "b"}, 20.0)

    assert out["access_token"] == "rotated"
    assert out["refresh_token"] == "rotated-r"
    assert saved["access_token"] == "rotated"
    assert import_calls["n"] == 0  # happy path must not consult ~/.codex


def test_rejected_refresh_never_persists_cli_half_token(monkeypatch):
    """A rejected refresh never reads or persists a half-token from CODEX_HOME."""
    saved = {}
    import_calls = {"n": 0}

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "fresh-only"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "invalid_grant"
    assert import_calls["n"] == 0
    assert saved == {}


def test_incomplete_singleton_self_heals_from_codex_cli(tmp_path, monkeypatch):
    """A present-but-incomplete provider is recovered from a valid Codex CLI store."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "stale-refresh"},
                "last_refresh": "2026-06-01T00:00:00Z",
                "auth_mode": "chatgpt",
                "last_auth_error": {
                    "code": "codex_auth_missing_access_token",
                    "message": "missing access token",
                },
            },
        },
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "fresh-access"
    assert resolved["source"] == "hermes-auth-store"
    stored = json.loads((hermes_home / "auth.json").read_text())
    provider = stored["providers"]["openai-codex"]
    assert provider["tokens"] == {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
    }
    assert "last_auth_error" not in provider


def test_self_heals_missing_provider_from_codex_cli(tmp_path, monkeypatch, caplog):
    """Hermes auth can be empty while Codex CLI still has a usable OAuth session."""
    caplog.set_level(logging.INFO, logger="hermes_cli.auth")
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "fresh-access"
    assert resolved["source"] == "hermes-auth-store"
    stored = json.loads((hermes_home / "auth.json").read_text())
    tokens = stored["providers"]["openai-codex"]["tokens"]
    assert tokens["access_token"] == "fresh-access"
    assert tokens["refresh_token"] == "fresh-refresh"
    assert "fresh-access" not in caplog.text
    assert "fresh-refresh" not in caplog.text


def test_empty_provider_self_heals_from_codex_cli(tmp_path, monkeypatch):
    """A present-but-empty provider is treated as recoverable, not authoritative."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {"openai-codex": {}},
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {
            "access_token": "cli-access-token",
            "refresh_token": "cli-refresh-token",
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved = resolve_codex_runtime_credentials(refresh_if_expiring=False)

    assert resolved["api_key"] == "cli-access-token"
    stored = json.loads((hermes_home / "auth.json").read_text())
    assert stored["providers"]["openai-codex"]["tokens"] == {
        "access_token": "cli-access-token",
        "refresh_token": "cli-refresh-token",
    }


def test_incomplete_singleton_prefers_usable_pool_over_codex_cli(tmp_path, monkeypatch):
    """An incomplete singleton must not bypass a usable Hermes pool credential."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "incomplete-refresh"},
                "auth_mode": "chatgpt",
            },
        },
        "credential_pool": {
            "openai-codex": [{
                "source": "manual:device_code",
                "access_token": "pool-access",
                "refresh_token": "pool-refresh",
            }],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    import_calls = {"count": 0}

    def _unexpected_import():
        import_calls["count"] += 1
        return {"access_token": "cli-access", "refresh_token": "cli-refresh"}

    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _unexpected_import)

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "pool-access"
    assert resolved["source"] == "credential_pool"
    assert import_calls["count"] == 0


def test_incomplete_singleton_pool_quota_wins_over_codex_cli(tmp_path, monkeypatch):
    """Pool quota is authoritative and must not be hidden by a CLI token import."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "incomplete-refresh"},
                "auth_mode": "chatgpt",
            },
        },
        "credential_pool": {
            "openai-codex": [{
                "access_token": "quota-access",
                "last_status": "exhausted",
                "last_error_code": 429,
                "last_error_reason": "usage_limit_reached",
                "last_error_reset_at": 9999999999,
            }],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    import_calls = {"count": 0}

    def _unexpected_import():
        import_calls["count"] += 1
        return {"access_token": "cli-access", "refresh_token": "cli-refresh"}

    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _unexpected_import)

    with pytest.raises(AuthError) as exc_info:
        resolve_codex_runtime_credentials()

    assert exc_info.value.code == auth.CODEX_RATE_LIMITED_CODE
    assert import_calls["count"] == 0


def test_missing_provider_cli_import_does_not_overwrite_concurrent_write(tmp_path, monkeypatch):
    """Revalidate under lock after CLI I/O and preserve a concurrent Hermes login."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def _import_with_concurrent_login():
        # CLI file I/O must happen outside the Hermes auth-store lock.
        assert getattr(auth._auth_lock_holder, "depth", 0) == 0
        auth_path.write_text(json.dumps({
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "concurrent-access",
                        "refresh_token": "concurrent-refresh",
                    },
                    "auth_mode": "chatgpt",
                },
            },
        }))
        return {"access_token": "cli-access", "refresh_token": "cli-refresh"}

    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_with_concurrent_login)

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "concurrent-access"
    stored = json.loads(auth_path.read_text())
    assert stored["providers"]["openai-codex"]["tokens"] == {
        "access_token": "concurrent-access",
        "refresh_token": "concurrent-refresh",
    }


def test_missing_singleton_access_token_reraises_when_codex_cli_half_token(tmp_path, monkeypatch):
    """Missing access_token must not be masked by a malformed Codex CLI import."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "stale-refresh"},
                "auth_mode": "chatgpt",
            },
        },
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": "fresh-only"},
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(AuthError) as ei:
        resolve_codex_runtime_credentials()

    assert ei.value.code == "codex_auth_missing_access_token"
