"""Hard guard for Hermes self-modification from messaging sessions.

The agent can be reached from many remote messaging surfaces.  Prompt rules are
not a security boundary, so mutations to Hermes' own code/config/profile state
must be denied deterministically unless the request came from a trusted origin
or from the local CLI.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Iterable


def _get_config_value(path: tuple[str, ...], default=None):
    try:
        from hermes_cli.config import cfg_get, load_config

        value = cfg_get(load_config(), *path)
        return default if value is None else value
    except Exception:
        return default


def _split_origins(raw) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    elif isinstance(raw, Iterable):
        parts = list(raw)
    else:
        parts = [raw]
    return {str(part).strip().lower() for part in parts if str(part).strip()}


def _allowed_gateway_origins() -> set[str]:
    origins = _split_origins(
        _get_config_value(("security", "self_modification_allowed_origins"), [])
    )
    origins.update(_split_origins(os.getenv("HERMES_SELF_MODIFICATION_ALLOWED_ORIGINS")))
    return origins


def _allowed_cron_jobs() -> set[str]:
    jobs = _split_origins(
        _get_config_value(("security", "self_modification_allowed_cron_jobs"), [])
    )
    jobs.update(_split_origins(os.getenv("HERMES_SELF_MODIFICATION_ALLOWED_CRON_JOBS")))
    return jobs


def _cron_values() -> tuple[str, str]:
    try:
        from gateway.session_context import get_session_env

        job_id = get_session_env("HERMES_CRON_JOB_ID", "") or ""
        job_name = get_session_env("HERMES_CRON_JOB_NAME", "") or ""
    except Exception:
        job_id = os.getenv("HERMES_CRON_JOB_ID", "") or ""
        job_name = os.getenv("HERMES_CRON_JOB_NAME", "") or ""
    return job_id.strip(), job_name.strip()


def _cron_script_values() -> tuple[str, str]:
    try:
        from gateway.session_context import get_session_env

        script = get_session_env("HERMES_CRON_SCRIPT", "") or ""
        script_sha256 = get_session_env("HERMES_CRON_SCRIPT_SHA256", "") or ""
    except Exception:
        script = os.getenv("HERMES_CRON_SCRIPT", "") or ""
        script_sha256 = os.getenv("HERMES_CRON_SCRIPT_SHA256", "") or ""
    return Path(script).name.strip(), script_sha256.strip().lower()


def _trusted_cron_script_manifest_path() -> Path | None:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "security" / "trusted-cron-scripts.json"
    except Exception:
        home = os.getenv("HERMES_HOME")
        if home:
            return Path(home) / "security" / "trusted-cron-scripts.json"
    return None


def _trusted_cron_script_entries() -> list[dict]:
    path = _trusted_cron_script_manifest_path()
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    entries = data.get("trusted") if isinstance(data, dict) else data
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _cron_script_manifest_allows(job_id: str, job_name: str) -> bool:
    script, script_sha256 = _cron_script_values()
    if not job_id or not script or not script_sha256:
        return False
    for entry in _trusted_cron_script_entries():
        entry_job_id = str(entry.get("job_id") or "").strip()
        entry_script = Path(str(entry.get("script") or "")).name.strip()
        entry_sha = str(entry.get("sha256") or "").strip().lower()
        if entry_job_id == job_id and entry_script == script and entry_sha == script_sha256:
            return True
    return False


def _cron_origin_is_self_modification_allowed() -> bool:
    job_id, job_name = _cron_values()
    if not job_id and not job_name:
        return False
    allowed = _allowed_cron_jobs()
    candidates = set()
    if job_id:
        candidates.update({job_id.lower(), f"cron:{job_id}".lower()})
    if job_name:
        candidates.update({job_name.lower(), f"cron:{job_name}".lower()})
    if candidates & allowed:
        return True
    return _cron_script_manifest_allows(job_id, job_name)


def _session_values() -> tuple[str, str]:
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or ""
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "") or ""
        chat_id = os.getenv("HERMES_SESSION_CHAT_ID", "") or ""
    return platform.strip().lower(), chat_id.strip()


def current_origin_is_self_modification_allowed() -> bool:
    """True when self-modification is allowed for the current execution origin.

    Local CLI/non-gateway sessions are allowed.  Cron and remote gateway sessions
    must match ``security.self_modification_allowed_origins`` entries such as
    ``whatsapp:1234567890``.  Matching accepts the exact chat id and the
    common WhatsApp JID suffix form (``...@g.us``) for the configured bare id.
    """
    if os.getenv("HERMES_CRON_SESSION"):
        # Cron has no human present and may have been created from an untrusted
        # remote origin; it may mutate Hermes only when the specific job is
        # explicitly allowlisted in config/env OR when the job id + script name +
        # script hash match the local trusted-cron-scripts manifest. This keeps
        # general remote-made cron jobs boxed out while allowing local
        # operational scripts that were deliberately enrolled by this instance.
        return _cron_origin_is_self_modification_allowed()

    platform, chat_id = _session_values()
    if not platform or platform in {"local", "cli"}:
        return True

    allowed = _allowed_gateway_origins()
    if not allowed:
        return False

    candidates = {f"{platform}:{chat_id}".lower()}
    if chat_id.endswith("@g.us"):
        candidates.add(f"{platform}:{chat_id[:-5]}".lower())
    else:
        candidates.add(f"{platform}:{chat_id}@g.us".lower())
    return bool(candidates & allowed)


def self_modification_denial(action: str) -> str | None:
    """Return a denial message when the current origin cannot mutate Hermes."""
    if current_origin_is_self_modification_allowed():
        return None
    platform, chat_id = _session_values()
    if os.getenv("HERMES_CRON_SESSION"):
        job_id, job_name = _cron_values()
        origin = f"cron:{job_id or job_name}" if (job_id or job_name) else "cron:unknown"
    else:
        origin = f"{platform}:{chat_id}" if platform or chat_id else "unknown/non-local"
    return (
        "Blocked by Hermes self-modification guard: this origin "
        f"({origin}) is not allowed to {action}. Changes to Hermes' own code, "
        "profile, skills, memories, cron jobs or config must come from the "
        "local CLI or from an explicitly trusted gateway origin configured in "
        "security.self_modification_allowed_origins."
    )


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except Exception:
        return path.expanduser().absolute()


def protected_self_roots() -> list[Path]:
    roots: list[Path] = []
    # Hermes source/install tree.  This file lives in <repo>/tools/.
    roots.append(_safe_resolve(Path(__file__).resolve().parents[1]))

    try:
        from hermes_constants import get_hermes_home

        roots.append(_safe_resolve(get_hermes_home()))
    except Exception:
        home = os.getenv("HERMES_HOME")
        if home:
            roots.append(_safe_resolve(Path(home)))

    explicit = _get_config_value(("security", "self_modification_protected_roots"), [])
    for item in _split_origins(explicit):
        roots.append(_safe_resolve(Path(item)))

    # Preserve order but drop duplicates.
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def path_is_protected_self_target(path: str | Path) -> bool:
    target = _safe_resolve(Path(path))
    for root in protected_self_roots():
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def check_file_self_modification(path: str | Path, action: str) -> str | None:
    if not path_is_protected_self_target(path):
        return None
    return self_modification_denial(action)


def _command_mentions_protected_root(command: str) -> bool:
    lowered = command.lower()
    if "hermes config set" in lowered or "hermes -p" in lowered or "hermes gateway" in lowered:
        return True
    for root in protected_self_roots():
        root_s = str(root)
        if root_s and (root_s in command or shlex.quote(root_s) in command):
            return True
    return False


def check_terminal_self_modification(command: str, env_type: str, cwd: str | None = None, workdir: str | None = None) -> str | None:
    """Hard-block local shell access from unauthorized remote origins.

    Shell commands are not reliably classifiable as read-only: ``python -c``,
    shell redirection, sourced scripts and encoded payloads can mutate protected
    state without an obvious path token.  Therefore unauthorized gateway/cron
    origins cannot use the local terminal at all.  Sandboxed/container backends
    remain outside this host-self-modification guard.
    """
    if env_type in {"docker", "singularity", "modal", "daytona", "vercel_sandbox"}:
        return None
    if current_origin_is_self_modification_allowed():
        return None
    return self_modification_denial("run local terminal commands")


def check_execute_code_self_modification(env_type: str) -> str | None:
    if env_type in {"docker", "singularity", "modal", "daytona", "vercel_sandbox"}:
        return None
    return self_modification_denial("run execute_code")
