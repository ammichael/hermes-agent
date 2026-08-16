"""Keep Browser Use off Safari when Chrome is missing.

uvx browser-use lives outside this repo. This file is injected via a
dedicated PYTHONPATH so upgrades don't revive the Safari fallback.
"""
import platform
import subprocess
import webbrowser


def _open_chrome_inspect():
    url = "chrome://inspect/#remote-debugging"
    if platform.system() == "Darwin":
        for app in ("Google Chrome", "Arc"):
            try:
                r = subprocess.run(
                    ["open", "-a", app, url],
                    timeout=5,
                    check=False,
                    capture_output=True,
                )
            except Exception:
                continue
            if r.returncode == 0:
                return True
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _patch():
    try:
        import browser_harness.admin as admin
    except Exception:
        return
    admin._open_chrome_inspect = _open_chrome_inspect


_patch()
