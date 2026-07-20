"""Tests for the DuckDuckGo (ddgs) web search provider.

Covers:
- DDGSWebSearchProvider.is_available() — reflects package importability
- DDGSWebSearchProvider.search() — happy path, missing package, runtime error
- Result normalization (title, url, description, position)
- _is_backend_available("ddgs") / _get_backend() integration
- web_extract returns a search-only error when ddgs is active
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from tests.tools.conftest import register_all_web_providers


def _install_fake_ddgs(monkeypatch, *, text_results=None, text_raises=None):
    """Install a stub ``ddgs`` module in sys.modules for the duration of a test.

    ``text_results``: iterable of dicts to yield from DDGS().text(...).
    ``text_raises``: if set, DDGS().text raises this exception instead.
    """

    fake = types.ModuleType("ddgs")

    class _FakeDDGS:
        def __init__(self, **kwargs):
            # Accept timeout= (and any other constructor kwargs) — the provider
            # now passes DDGS(timeout=10).
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def text(self, query, max_results=5):
            if text_raises is not None:
                raise text_raises
            for hit in (text_results or []):
                yield hit

    fake.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    return fake


def _run_fake_ddgs_inline(monkeypatch, provider_class):
    """Keep normalization unit tests in-process; isolation has dedicated tests."""
    search_globals = provider_class.search.__globals__
    monkeypatch.setitem(
        search_globals,
        "_run_ddgs_search_isolated",
        search_globals["_run_ddgs_search"],
    )


def _native_gil_blocking_worker(send_conn, query, safe_limit):
    """Hold the child interpreter's GIL in native code until the parent kills it."""
    import ctypes
    import sys

    if sys.platform == "win32":
        kernel32 = ctypes.PyDLL("kernel32.dll")
        kernel32.Sleep.argtypes = [ctypes.c_uint32]
        kernel32.Sleep.restype = None
        kernel32.Sleep(60_000)
    else:
        ctypes.PyDLL(None).sleep(60)


def _successful_envelope_worker(send_conn, query, safe_limit):
    send_conn.send(("ok", [{"title": query, "position": safe_limit}]))
    send_conn.close()


def _error_envelope_worker(send_conn, query, safe_limit):
    send_conn.send(("error", "RuntimeError: isolated boom"))
    send_conn.close()


# ---------------------------------------------------------------------------
# DDGSWebSearchProvider unit tests
# ---------------------------------------------------------------------------


class TestDDGSProviderIsConfigured:
    def test_configured_when_package_importable(self, monkeypatch):
        _install_fake_ddgs(monkeypatch)
        # Drop any cached ``plugins.web.ddgs.provider`` so is_configured re-imports ddgs fresh
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().is_available() is True

    def test_not_configured_when_package_missing(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        # Block the import so ``import ddgs`` raises ImportError even if the package is actually installed
        import builtins
        orig_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("blocked for test")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().is_available() is False

    def test_provider_name(self):
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert DDGSWebSearchProvider().name == "ddgs"

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        assert issubclass(DDGSWebSearchProvider, WebSearchProvider)


class TestDDGSProviderSearch:
    def test_happy_path_normalizes_results(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": "A", "href": "https://a.example.com", "body": "desc A"},
            {"title": "B", "href": "https://b.example.com", "body": "desc B"},
            {"title": "C", "href": "https://c.example.com", "body": "desc C"},
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 3
        assert web[0] == {"title": "A", "url": "https://a.example.com", "description": "desc A", "position": 1}
        assert web[2]["position"] == 3

    def test_accepts_url_key_as_fallback_for_href(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": "A", "url": "https://a.example.com", "body": "desc A"},
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert result["data"]["web"][0]["url"] == "https://a.example.com"

    def test_limit_is_respected(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[
            {"title": f"R{i}", "href": f"https://r{i}.example.com", "body": ""}
            for i in range(10)
        ])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("q", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 3

    def test_missing_package_returns_failure(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "ddgs", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.web.ddgs.provider", raising=False)
        import builtins
        orig_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("blocked for test")
            return orig_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        from plugins.web.ddgs.provider import DDGSWebSearchProvider

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is False
        assert "ddgs" in result["error"].lower()

    def test_runtime_error_returns_failure(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_raises=RuntimeError("rate limited 202"))
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is False
        assert "rate limited" in result["error"] or "failed" in result["error"].lower()

    def test_empty_results(self, monkeypatch):
        _install_fake_ddgs(monkeypatch, text_results=[])
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("nothing", limit=5)
        assert result["success"] is True
        assert result["data"]["web"] == []

    def test_hung_search_times_out_and_returns_failure(self, monkeypatch):
        """The provider maps an isolated-worker deadline to a useful tool error."""
        import plugins.web.ddgs.provider as _prov

        _install_fake_ddgs(monkeypatch)

        def _timeout(query, safe_limit):
            raise _prov._DDGSSearchTimeout

        monkeypatch.setattr(_prov, "_run_ddgs_search_isolated", _timeout)
        monkeypatch.setattr(_prov, "_SEARCH_TIMEOUT_SECS", 0.3)

        result = _prov.DDGSWebSearchProvider().search("hangs forever", limit=5)

        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_interrupted_search_returns_interrupted_error(self, monkeypatch):
        import plugins.web.ddgs.provider as _prov

        _install_fake_ddgs(monkeypatch)

        def _interrupted(query, safe_limit):
            raise _prov._DDGSSearchInterrupted

        monkeypatch.setattr(_prov, "_run_ddgs_search_isolated", _interrupted)

        result = _prov.DDGSWebSearchProvider().search("cancel me", limit=5)

        assert result == {"success": False, "error": "Interrupted"}

    def test_isolation_times_out_native_worker_holding_gil(self):
        """Regression: native code holding the child GIL cannot freeze Hermes."""
        import multiprocessing
        import time

        import plugins.web.ddgs.provider as _prov

        start = time.monotonic()
        with pytest.raises(_prov._DDGSSearchTimeout):
            _prov._run_ddgs_search_isolated(
                "native hang",
                5,
                timeout=0.3,
                worker_target=_native_gil_blocking_worker,
            )
        elapsed = time.monotonic() - start

        assert elapsed < 3.0, f"isolated search did not return promptly ({elapsed:.1f}s)"
        assert not any(
            child.name == "hermes-ddgs-search"
            for child in multiprocessing.active_children()
        )

    def test_isolation_interrupts_and_reaps_native_worker_promptly(self):
        """Gateway/TUI cancellation must not wait for the full search timeout."""
        import multiprocessing
        import threading
        import time

        import plugins.web.ddgs.provider as _prov
        from tools.interrupt import set_interrupt

        owner_thread = threading.get_ident()
        interrupter = threading.Timer(0.1, set_interrupt, args=(True, owner_thread))
        interrupter.start()
        try:
            start = time.monotonic()
            with pytest.raises(_prov._DDGSSearchInterrupted):
                _prov._run_ddgs_search_isolated(
                    "interrupt native hang",
                    5,
                    timeout=10,
                    worker_target=_native_gil_blocking_worker,
                )
            elapsed = time.monotonic() - start
        finally:
            interrupter.cancel()
            set_interrupt(False, owner_thread)

        assert elapsed < 2.0, f"interrupt did not stop the worker promptly ({elapsed:.1f}s)"
        assert not any(
            child.name == "hermes-ddgs-search"
            for child in multiprocessing.active_children()
        )

    def test_isolation_decodes_success_envelope_and_reaps_worker(self):
        import multiprocessing

        import plugins.web.ddgs.provider as _prov

        result = _prov._run_ddgs_search_isolated(
            "spawned result",
            3,
            timeout=3,
            worker_target=_successful_envelope_worker,
        )

        assert result == [{"title": "spawned result", "position": 3}]
        assert not any(
            child.name == "hermes-ddgs-search"
            for child in multiprocessing.active_children()
        )

    def test_isolation_decodes_error_envelope_and_reaps_worker(self):
        import multiprocessing

        import plugins.web.ddgs.provider as _prov

        with pytest.raises(RuntimeError, match="isolated boom"):
            _prov._run_ddgs_search_isolated(
                "spawned error",
                3,
                timeout=3,
                worker_target=_error_envelope_worker,
            )

        assert not any(
            child.name == "hermes-ddgs-search"
            for child in multiprocessing.active_children()
        )

    def test_fast_search_not_affected_by_timeout_wrapper(self, monkeypatch):
        """Happy-path guard: the timeout wrapper must not break a normal,
        fast search — results flow through unchanged."""
        _install_fake_ddgs(
            monkeypatch,
            text_results=[{"title": "T", "href": "https://e.com", "body": "B"}],
        )
        from plugins.web.ddgs.provider import DDGSWebSearchProvider
        _run_fake_ddgs_inline(monkeypatch, DDGSWebSearchProvider)

        result = DDGSWebSearchProvider().search("q", limit=5)
        assert result["success"] is True
        assert result["data"]["web"][0]["url"] == "https://e.com"
        assert result["data"]["web"][0]["title"] == "T"


# ---------------------------------------------------------------------------
# Integration: _is_backend_available / _get_backend / check_web_api_key
# ---------------------------------------------------------------------------


class TestDDGSBackendWiring:
    def test_is_backend_available_true_when_package_importable(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._is_backend_available("ddgs") is True

    def test_is_backend_available_false_when_package_missing(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
        assert web_tools._is_backend_available("ddgs") is False

    def test_configured_backend_accepted(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "ddgs"

    def test_ddgs_trails_paid_providers_in_auto_detect(self, monkeypatch):
        """Exa (priority) should win over ddgs in auto-detect."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
                    "TAVILY_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("EXA_API_KEY", "exa-key")
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "exa"

    def test_auto_detect_picks_ddgs_as_last_resort(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        for key in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "PARALLEL_API_KEY",
                    "TAVILY_API_KEY", "EXA_API_KEY", "SEARXNG_URL", "BRAVE_SEARCH_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools._get_backend() == "ddgs"

    def test_check_web_api_key_true_when_ddgs_configured(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        assert web_tools.check_web_api_key() is True


# ---------------------------------------------------------------------------
# ddgs is search-only: web_extract returns a clear error
# ---------------------------------------------------------------------------


class TestDDGSSearchOnlyErrors:
    _register_providers = staticmethod(register_all_web_providers)

    @pytest.fixture(autouse=True)
    def _populate_web_registry(self):
        self._register_providers()
        yield
        from agent.web_search_registry import _reset_for_tests
        _reset_for_tests()

    def test_web_extract_returns_search_only_error(self, monkeypatch):
        import asyncio
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "ddgs"})
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: True)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        async def _allow_ssrf(_url: str) -> bool:
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_ssrf)
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        result_str = asyncio.get_event_loop().run_until_complete(
            web_tools.web_extract_tool(["https://example.com"])
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "search-only" in result["error"].lower()
        assert "duckduckgo" in result["error"].lower() or "ddgs" in result["error"].lower()
