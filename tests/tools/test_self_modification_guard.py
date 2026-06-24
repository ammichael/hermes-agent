from pathlib import Path

from gateway.session_context import _VAR_MAP, clear_session_vars, set_session_vars
from tools.self_modification_guard import (
    check_execute_code_self_modification,
    check_file_self_modification,
    check_terminal_self_modification,
    current_origin_is_self_modification_allowed,
)


def _with_session(platform: str, chat_id: str):
    return set_session_vars(platform=platform, chat_id=chat_id, async_delivery=True)


def test_cli_origin_can_modify_self(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    assert current_origin_is_self_modification_allowed() is True
    assert check_file_self_modification(tmp_path / "profile" / "config.yaml", "write") is None
    assert check_terminal_self_modification("python -V", "local") is None
    assert check_execute_code_self_modification("local") is None


def test_untrusted_gateway_origin_cannot_modify_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    tokens = _with_session("slack", "D123")
    try:
        protected = tmp_path / "profile" / "config.yaml"
        denial = check_file_self_modification(protected, "write")
        assert denial is not None
        assert "Blocked by Hermes self-modification guard" in denial
        assert check_terminal_self_modification("date", "local") is not None
        assert check_execute_code_self_modification("local") is not None
    finally:
        clear_session_vars(tokens)


def test_trusted_whatsapp_origin_can_modify_self(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS", "whatsapp:1234567890")
    tokens = _with_session("whatsapp", "1234567890@g.us")
    try:
        assert current_origin_is_self_modification_allowed() is True
        assert check_file_self_modification(tmp_path / "profile" / "config.yaml", "write") is None
        assert check_terminal_self_modification("date", "local") is None
        assert check_execute_code_self_modification("local") is None
    finally:
        clear_session_vars(tokens)


def test_trusted_whatsapp_direct_lid_origin_can_modify_self(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS", "whatsapp:trusted-user@lid")
    tokens = _with_session("whatsapp", "trusted-user@lid")
    try:
        assert current_origin_is_self_modification_allowed() is True
        assert check_file_self_modification(tmp_path / "profile" / "config.yaml", "write") is None
        assert check_terminal_self_modification("date", "local") is None
        assert check_execute_code_self_modification("local") is None
    finally:
        clear_session_vars(tokens)


def test_untrusted_gateway_can_still_write_non_self_path(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    tokens = _with_session("slack", "D123")
    try:
        assert check_file_self_modification(tmp_path / "workspace" / "app.py", "write") is None
    finally:
        clear_session_vars(tokens)


def test_cron_never_modifies_self_even_without_gateway_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    protected = Path(tmp_path / "profile" / "config.yaml")

    denial = check_file_self_modification(protected, "write")
    assert denial is not None
    assert "cron:unknown" in denial
    assert check_terminal_self_modification("date", "local") is not None


def test_explicitly_trusted_cron_job_can_modify_self(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "299c94e42f89")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", "299c94e42f89")
    protected = Path(tmp_path / "profile" / "config.yaml")

    assert current_origin_is_self_modification_allowed() is True
    assert check_file_self_modification(protected, "write") is None
    assert check_terminal_self_modification("date", "local") is None
    assert check_execute_code_self_modification("local") is None


def test_untrusted_cron_job_remains_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "untrusted")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", "299c94e42f89")
    protected = Path(tmp_path / "profile" / "config.yaml")

    denial = check_file_self_modification(protected, "write")
    assert denial is not None
    assert "cron:untrusted" in denial
    assert check_terminal_self_modification("date", "local") is not None


def test_trusted_cron_job_contextvar_can_modify_self(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_CRON_JOB_ID", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", "299c94e42f89")
    _VAR_MAP["HERMES_CRON_JOB_ID"].set("299c94e42f89")
    _VAR_MAP["HERMES_CRON_JOB_NAME"].set("GitHub Deep Review Watcher")
    try:
        assert current_origin_is_self_modification_allowed() is True
        assert check_terminal_self_modification("date", "local") is None
    finally:
        _VAR_MAP["HERMES_CRON_JOB_ID"].set("")
        _VAR_MAP["HERMES_CRON_JOB_NAME"].set("")


def test_trusted_cron_script_manifest_allows_matching_job_and_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", raising=False)
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    manifest_dir = profile / "security"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "trusted-cron-scripts.json"
    manifest.write_text(
        '{"trusted":[{"job_id":"job-1","script":"local-maintenance.py","sha256":"abc123"}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "job-1")
    monkeypatch.setenv("HERMES_CRON_SCRIPT", "local-maintenance.py")
    monkeypatch.setenv("HERMES_CRON_SCRIPT_SHA256", "abc123")
    _VAR_MAP["HERMES_CRON_JOB_ID"].set("job-1")
    _VAR_MAP["HERMES_CRON_SCRIPT"].set("local-maintenance.py")
    _VAR_MAP["HERMES_CRON_SCRIPT_SHA256"].set("abc123")
    try:
        assert current_origin_is_self_modification_allowed() is True
        assert check_terminal_self_modification("date", "local") is None
    finally:
        _VAR_MAP["HERMES_CRON_JOB_ID"].set("")
        _VAR_MAP["HERMES_CRON_SCRIPT"].set("")
        _VAR_MAP["HERMES_CRON_SCRIPT_SHA256"].set("")


def test_trusted_cron_script_manifest_rejects_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS", raising=False)
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    manifest_dir = profile / "security"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "trusted-cron-scripts.json"
    manifest.write_text(
        '{"trusted":[{"job_id":"job-1","script":"local-maintenance.py","sha256":"abc123"}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "job-1")
    monkeypatch.setenv("HERMES_CRON_SCRIPT", "local-maintenance.py")
    monkeypatch.setenv("HERMES_CRON_SCRIPT_SHA256", "def456")
    _VAR_MAP["HERMES_CRON_JOB_ID"].set("job-1")
    _VAR_MAP["HERMES_CRON_SCRIPT"].set("local-maintenance.py")
    _VAR_MAP["HERMES_CRON_SCRIPT_SHA256"].set("def456")
    try:
        denial = check_terminal_self_modification("date", "local")
        assert denial is not None
        assert "cron:job-1" in denial
    finally:
        _VAR_MAP["HERMES_CRON_JOB_ID"].set("")
        _VAR_MAP["HERMES_CRON_SCRIPT"].set("")
        _VAR_MAP["HERMES_CRON_SCRIPT_SHA256"].set("")
