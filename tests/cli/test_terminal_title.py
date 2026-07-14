"""Cross-terminal session-title synchronization."""

import threading
from unittest.mock import MagicMock, patch

from hermes_cli.terminal_title import normalize_terminal_title, set_terminal_title


def test_normalize_terminal_title_removes_controls_and_bounds_words():
    title = normalize_terminal_title("  App\nStore\x1b Estrago  ")
    assert title == "App Store Estrago"


def test_standard_terminal_writes_osc_to_controlling_tty():
    with (
        patch("hermes_cli.terminal_title.os.ctermid", return_value="/dev/ttys009"),
        patch("hermes_cli.terminal_title.os.open", return_value=41) as open_mock,
        patch("hermes_cli.terminal_title.os.write") as write_mock,
        patch("hermes_cli.terminal_title.os.close") as close_mock,
        patch("hermes_cli.terminal_title.shutil.which", return_value=None),
    ):
        assert set_terminal_title("App Store Estrago", environ={"TERM": "xterm-256color"}) is True

    open_mock.assert_called_once()
    assert open_mock.call_args.args[0] == "/dev/ttys009"
    write_mock.assert_called_once_with(41, b"\x1b]0;App Store Estrago\x07")
    close_mock.assert_called_once_with(41)


def test_dumb_or_missing_terminal_skips_osc():
    with patch("hermes_cli.terminal_title.os.open") as open_mock:
        assert set_terminal_title("App Store Estrago", environ={"TERM": "dumb"}) is False
    open_mock.assert_not_called()


def test_cmux_renames_exact_caller_workspace_and_keeps_osc_fallback():
    run = MagicMock()

    def which(name):
        return "/opt/cmux/bin/cmux" if name == "cmux" else None

    with (
        patch("hermes_cli.terminal_title.shutil.which", side_effect=which),
        patch("hermes_cli.terminal_title.subprocess.run", run),
        patch("hermes_cli.terminal_title.os.ctermid", return_value="/dev/ttys010"),
        patch("hermes_cli.terminal_title.os.open", return_value=42),
        patch("hermes_cli.terminal_title.os.write") as write_mock,
        patch("hermes_cli.terminal_title.os.close"),
    ):
        assert set_terminal_title(
            "Títulos de Sessão",
            environ={
                "TERM": "xterm-ghostty",
                "CMUX_WORKSPACE_ID": "workspace:7",
                "CMUX_SURFACE_ID": "surface:2",
            },
        ) is True

    args = run.call_args.args[0]
    assert args == [
        "/opt/cmux/bin/cmux",
        "rename-workspace",
        "--workspace",
        "workspace:7",
        "--",
        "Títulos de Sessão",
    ]
    assert run.call_args.kwargs["timeout"] == 1.0
    write_mock.assert_called_once_with(42, "\x1b]0;Títulos de Sessão\x07".encode())


def test_tmux_renames_current_window_without_shell_interpolation():
    run = MagicMock()
    run.return_value.returncode = 0

    def which(name):
        return "/usr/bin/tmux" if name == "tmux" else None

    with (
        patch("hermes_cli.terminal_title.shutil.which", side_effect=which),
        patch("hermes_cli.terminal_title.subprocess.run", run),
        patch("hermes_cli.terminal_title.os.ctermid", side_effect=OSError),
    ):
        assert set_terminal_title(
            "Crash Produção Estrago",
            environ={"TERM": "screen-256color", "TMUX": "/tmp/tmux,1,0"},
        ) is True

    assert run.call_args.args[0] == ["/usr/bin/tmux", "rename-window", "Crash Produção Estrago"]


def test_command_and_tty_failures_are_silent():
    with (
        patch("hermes_cli.terminal_title.shutil.which", return_value="/opt/cmux/bin/cmux"),
        patch("hermes_cli.terminal_title.subprocess.run", side_effect=OSError),
        patch("hermes_cli.terminal_title.os.ctermid", side_effect=OSError),
    ):
        assert set_terminal_title(
            "App Store Estrago",
            environ={"TERM": "xterm", "CMUX_WORKSPACE_ID": "workspace:1"},
        ) is False


def test_cli_title_sync_ignores_stale_async_session():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "new-session"
    with patch("hermes_cli.terminal_title.set_terminal_title") as setter:
        assert cli._sync_terminal_session_title(
            "Old Session Topic",
            expected_session_id="old-session",
        ) is False
    setter.assert_not_called()


def test_cli_title_sync_targets_current_session():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "current-session"
    with patch(
        "hermes_cli.terminal_title.set_terminal_title",
        return_value=True,
    ) as setter:
        assert cli._sync_terminal_session_title(
            "App Store Estrago",
            expected_session_id="current-session",
        ) is True
    setter.assert_called_once_with("App Store Estrago")


def test_async_title_that_started_first_cannot_finish_after_resumed_title():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "old-session"
    cli._terminal_title_lock = threading.RLock()
    cli._session_db = MagicMock()
    cli._session_db.get_session_title.return_value = "Old Async Title"
    old_entered = threading.Event()
    release_old = threading.Event()
    completion_order = []

    def blocking_setter(title):
        if title == "Old Async Title":
            old_entered.set()
            assert release_old.wait(2)
        completion_order.append(title)
        return True

    with patch(
        "hermes_cli.terminal_title.set_terminal_title",
        side_effect=blocking_setter,
    ):
        old_worker = threading.Thread(
            target=cli._sync_terminal_session_title,
            args=("Old Async Title",),
            kwargs={
                "expected_session_id": "old-session",
                "require_persisted_title": True,
            },
        )
        old_worker.start()
        assert old_entered.wait(2)

        cli.session_id = "new-session"
        new_worker = threading.Thread(
            target=cli._sync_terminal_session_title,
            args=("New Resume Title",),
        )
        new_worker.start()
        release_old.set()
        old_worker.join(2)
        new_worker.join(2)

    assert not old_worker.is_alive()
    assert not new_worker.is_alive()
    assert completion_order == ["Old Async Title", "New Resume Title"]


def test_async_title_must_still_be_the_persisted_title():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "current-session"
    cli._terminal_title_lock = threading.RLock()
    cli._session_db = MagicMock()
    cli._session_db.get_session_title.return_value = "Manual Session Title"

    with patch("hermes_cli.terminal_title.set_terminal_title") as setter:
        assert cli._sync_terminal_session_title(
            "Auto Generated Title",
            expected_session_id="current-session",
            require_persisted_title=True,
        ) is False
    setter.assert_not_called()


def test_manual_title_updates_session_and_terminal():
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "current-session"
    cli._session_db = MagicMock()
    cli._session_db.get_session.return_value = {"id": "current-session"}
    cli._session_db.set_session_title.return_value = True
    cli._pending_title = None
    cli._sync_terminal_session_title = MagicMock()

    with patch("cli._cprint"):
        cli.process_command("/title App Store Estrago")

    cli._session_db.set_session_title.assert_called_once_with(
        "current-session", "App Store Estrago"
    )
    cli._sync_terminal_session_title.assert_called_once_with("App Store Estrago")
