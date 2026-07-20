"""DuckDuckGo search — plugin form (via the ``ddgs`` package).

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.
The legacy in-tree module ``tools.web_providers.ddgs`` was removed in the
same commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

The ``ddgs`` package is an optional dependency. ``is_available()`` reflects
whether the package is importable; the plugin still registers either way so
``hermes tools`` can prompt the user to install it.
"""

from __future__ import annotations

import logging
import multiprocessing as _mp
import time
from multiprocessing.connection import Connection
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Overall wall-clock cap for a single ddgs search. The DDGS constructor's
# ``timeout`` only bounds individual HTTP requests; ddgs's multi-engine retry
# loop has no overall cap. More importantly, its native ``primp`` transport can
# hold the Python GIL while blocked, which prevents a thread-based watchdog,
# signal handlers, and the CLI interrupt loop from running. Execute each search
# in a disposable spawned process so the parent remains responsive and can
# forcibly stop the native call at the deadline.
_SEARCH_TIMEOUT_SECS = 30
_PROCESS_CLEANUP_SECS = 1.0
_INTERRUPT_POLL_SECS = 0.05


class _DDGSSearchTimeout(TimeoutError):
    """Raised when the isolated DDGS worker exceeds its wall-clock budget."""


class _DDGSSearchInterrupted(RuntimeError):
    """Raised when Hermes cancels the thread that owns the DDGS search."""


def _run_ddgs_search(query: str, safe_limit: int) -> list[dict[str, Any]]:
    """Run and normalize one blocking ddgs query inside the worker process."""
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(client.text(query, max_results=safe_limit)):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


def _ddgs_worker_entry(
    send_conn: Connection,
    query: str,
    safe_limit: int,
) -> None:
    """Execute DDGS in a child and send a small, pickle-safe result envelope."""
    try:
        send_conn.send(("ok", _run_ddgs_search(query, safe_limit)))
    except BaseException as exc:  # child must report provider/native failures
        send_conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        send_conn.close()


def _stop_worker(process: Any) -> None:
    """Reap a worker, escalating from terminate to kill when necessary."""
    if process.pid is None:
        return
    if not process.is_alive():
        process.join(timeout=_PROCESS_CLEANUP_SECS)
        return
    process.terminate()
    process.join(timeout=_PROCESS_CLEANUP_SECS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_PROCESS_CLEANUP_SECS)


def _run_ddgs_search_isolated(
    query: str,
    safe_limit: int,
    *,
    timeout: float | None = None,
    worker_target: Any = None,
) -> list[dict[str, Any]]:
    """Run DDGS in a spawned process that can be killed even if native code holds the GIL."""
    deadline = _SEARCH_TIMEOUT_SECS if timeout is None else timeout
    target = _ddgs_worker_entry if worker_target is None else worker_target
    context = _mp.get_context("spawn")
    recv_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=target,
        args=(send_conn, query, safe_limit),
        name="hermes-ddgs-search",
        daemon=True,
    )
    try:
        process.start()
        send_conn.close()
        deadline_at = time.monotonic() + max(0.0, deadline)
        from tools.interrupt import is_interrupted

        while True:
            if is_interrupted():
                raise _DDGSSearchInterrupted
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise _DDGSSearchTimeout
            if recv_conn.poll(min(_INTERRUPT_POLL_SECS, remaining)):
                break
        try:
            status, payload = recv_conn.recv()
        except EOFError as exc:
            raise RuntimeError(
                f"DDGS worker exited without a result (exit code {process.exitcode})"
            ) from exc
        if status == "error":
            raise RuntimeError(str(payload))
        if status != "ok" or not isinstance(payload, list):
            raise RuntimeError("DDGS worker returned an invalid result")
        return payload
    finally:
        recv_conn.close()
        send_conn.close()
        _stop_worker(process)


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo HTML-scrape search provider.

    No API key needed. Rate limits are enforced server-side by DuckDuckGo;
    the provider surfaces ``DuckDuckGoSearchException`` and other ddgs errors
    as ``{"success": False, "error": ...}`` rather than raising.
    """

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo (ddgs)"

    def is_available(self) -> bool:
        """Return True when the ``ddgs`` package is importable.

        Probes the import once; cheap because Python caches the import. Must
        NOT perform network I/O — runs at tool-registration time and on every
        ``hermes tools`` paint.
        """
        try:
            import ddgs  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a DuckDuckGo search and return normalized results.

        The synchronous ``ddgs`` call is run in a spawned process with a hard
        wall-clock timeout (``_SEARCH_TIMEOUT_SECS``). Process isolation is
        required because the native transport can block while holding the GIL.
        """
        try:
            import ddgs  # type: ignore  # noqa: F401 — availability probe
        except ImportError:
            return {
                "success": False,
                "error": "ddgs package is not installed — run `pip install ddgs`",
            }

        # DDGS().text yields at most `max_results` items; we cap defensively
        # in case the package ignores the hint.
        safe_limit = max(1, int(limit))

        try:
            web_results = _run_ddgs_search_isolated(query, safe_limit)
        except _DDGSSearchInterrupted:
            logger.info("DDGS search interrupted")
            return {"success": False, "error": "Interrupted"}
        except _DDGSSearchTimeout:
            logger.warning(
                "DDGS search timed out after %ds for query: %r",
                _SEARCH_TIMEOUT_SECS, query,
            )
            return {
                "success": False,
                "error": (
                    f"DuckDuckGo search timed out after {_SEARCH_TIMEOUT_SECS}s — "
                    "DuckDuckGo may be rate-limiting or slow. Try again later "
                    "or switch to a different search provider."
                ),
            }
        except Exception as exc:  # noqa: BLE001 — worker reports provider/native errors
            logger.warning("DDGS search error: %s", exc)
            return {"success": False, "error": f"DuckDuckGo search failed: {exc}"}

        logger.info("DDGS search '%s': %d results (limit %d)", query, len(web_results), limit)
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free · no key · search only",
            "tag": "Search via the ddgs Python package — no API key (pair with any extract provider)",
            "env_vars": [],
            # Trigger `_run_post_setup("ddgs")` after the user picks this row
            # so the ddgs Python package gets pip-installed on first selection.
            "post_setup": "ddgs",
        }
