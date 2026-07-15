"""Regression tests for machine-clean single-query quiet mode."""

from types import SimpleNamespace

from cli import _configure_single_query_quiet_mode


def test_quiet_mode_disables_inline_diff_stdout():
    cli = SimpleNamespace(tool_progress_mode="all", _inline_diffs_enabled=True)

    _configure_single_query_quiet_mode(cli)

    assert cli.tool_progress_mode == "off"
    assert cli._inline_diffs_enabled is False
