from pathlib import Path
from unittest.mock import MagicMock, patch


def test_packaged_desktop_launch_is_detached_and_silent_on_posix():
    from hermes_cli.main import _launch_packaged_desktop_detached

    proc = MagicMock(pid=4321)
    with patch("hermes_cli.main.sys.platform", "darwin"), \
         patch("hermes_cli.main.subprocess.Popen", return_value=proc) as popen:
        pid = _launch_packaged_desktop_detached(
            ["/tmp/Hermes.app/Contents/MacOS/Hermes"],
            cwd=Path("/tmp"),
            env={"HOME": "/tmp"},
        )

    assert pid == 4321
    kwargs = popen.call_args.kwargs
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
