"""Regression coverage for lightweight local Holographic hardening."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("numpy")

from plugins.memory.holographic import HolographicMemoryProvider


def _provider(tmp_path, **config):
    values = {
        "db_path": str(tmp_path / "memory.db"),
        "auto_extract": "false",
        "auto_prefetch": "false",
        "mirror_memory_writes": "false",
        "prefetch_min_score": 0.25,
        "prefetch_limit": 2,
        "hrr_weight": 0.0,
        "default_trust": 0.8,
        **config,
    }
    provider = HolographicMemoryProvider(values)
    provider.initialize("test-session")
    return provider


def test_prefetch_is_explicit_only_when_disabled(tmp_path):
    provider = _provider(tmp_path)
    try:
        assert provider._store is not None
        provider._store.add_fact(
            "Rafa prefere respostas diretas, completas e sem bajulação."
        )
        assert provider.prefetch("respostas diretas Rafa") == ""

        payload = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "search", "query": "respostas diretas Rafa"}
        ))
        assert payload["count"] == 1
        assert "respostas diretas" in payload["results"][0]["content"]
    finally:
        provider.shutdown()


def test_enabled_prefetch_filters_low_score_incidental_matches(tmp_path):
    provider = _provider(tmp_path, auto_prefetch="true")
    try:
        assert provider._store is not None
        provider._store.add_fact(
            "Rafa prefere respostas diretas, completas e sem bajulação."
        )
        provider._store.add_fact(
            "Lia, também chamada Bia, é esposa de Rafa."
        )

        result = provider.prefetch("respostas diretas Rafa")
        assert "respostas diretas" in result
        assert "esposa de Rafa" not in result
    finally:
        provider.shutdown()


def test_portuguese_prose_does_not_match_common_word_only(tmp_path):
    provider = _provider(tmp_path, auto_prefetch="true")
    try:
        assert provider._store is not None
        provider._store.add_fact(
            "O projeto Atlas usa SQLite como banco canônico."
        )
        assert provider.prefetch("Como ele gosta que eu responda?") == ""
    finally:
        provider.shutdown()


def test_string_false_disables_extraction_and_memory_mirroring(tmp_path):
    provider = _provider(
        tmp_path,
        auto_extract="false",
        mirror_memory_writes="false",
    )
    try:
        provider.on_session_end([
            {"role": "user", "content": "I prefer that every response be concise."}
        ])
        provider.on_memory_write("add", "user", "User prefers concise responses.")
        assert provider._store is not None
        assert provider._store.list_facts(limit=20) == []
    finally:
        provider.shutdown()


def test_explicit_true_can_enable_memory_mirroring(tmp_path):
    provider = _provider(tmp_path, mirror_memory_writes="true")
    try:
        provider.on_memory_write("add", "user", "User prefers concise responses.")
        assert provider._store is not None
        facts = provider._store.list_facts(limit=20)
        assert [fact["content"] for fact in facts] == ["User prefers concise responses."]
    finally:
        provider.shutdown()
