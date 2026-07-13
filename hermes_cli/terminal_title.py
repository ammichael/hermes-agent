"""Best-effort terminal/session title synchronization for the interactive CLI.

The generic path writes the standard OSC 0 sequence to the process's own
controlling TTY.  That naturally targets the terminal surface hosting this
Hermes instance instead of whichever application window happens to be focused.
Terminal-specific integrations are additive: cmux workspace names and tmux
window names are useful navigation labels that OSC alone does not always
update.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unicodedata
from collections.abc import Mapping

_MAX_TITLE_CHARS = 100
_COMMAND_TIMEOUT_SECONDS = 1.0


def normalize_terminal_title(title: str) -> str:
    """Return a bounded, single-line title safe for OSC and command arguments."""
    if not isinstance(title, str):
        return ""
    chars = []
    for ch in title:
        category = unicodedata.category(ch)
        if ch.isspace():
            chars.append(" ")
        elif category not in {"Cc", "Cf", "Cs"}:
            chars.append(ch)
    printable = "".join(chars)
    return " ".join(printable.split())[:_MAX_TITLE_CHARS].strip()


def _run_quietly(argv: list[str]) -> bool:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _rename_cmux_workspace(title: str, environ: Mapping[str, str]) -> bool:
    workspace_id = environ.get("CMUX_WORKSPACE_ID", "").strip()
    executable = shutil.which("cmux")
    if not workspace_id or not executable:
        return False
    return _run_quietly(
        [executable, "rename-workspace", "--workspace", workspace_id, "--", title]
    )


def _rename_tmux_window(title: str, environ: Mapping[str, str]) -> bool:
    if not environ.get("TMUX"):
        return False
    executable = shutil.which("tmux")
    if not executable:
        return False
    return _run_quietly([executable, "rename-window", title])


def _write_osc_title(title: str, environ: Mapping[str, str]) -> bool:
    if environ.get("TERM", "").lower() in {"", "dumb", "unknown"}:
        return False

    fd = None
    try:
        fd = os.open(
            os.ctermid(),
            os.O_WRONLY | getattr(os, "O_NOCTTY", 0),
        )
        os.write(fd, f"\x1b]0;{title}\x07".encode("utf-8"))
        return True
    except (AttributeError, OSError):
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def set_terminal_title(
    title: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Synchronize *title* to the current terminal context, best effort.

    Returns True when at least one supported target accepted the update.  All
    failures are intentionally silent: title decoration must never interfere
    with the chat loop or session persistence.
    """
    clean_title = normalize_terminal_title(title)
    if not clean_title:
        return False

    env = os.environ if environ is None else environ
    results = (
        _rename_cmux_workspace(clean_title, env),
        _rename_tmux_window(clean_title, env),
        _write_osc_title(clean_title, env),
    )
    return any(results)
