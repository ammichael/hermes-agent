"""Plugin de plataforma companion: backfill, devices, apns, watcher, adapter."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))


def _make_sessions_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.executemany("INSERT INTO sessions (id, source) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


class TestBackfillSource:
    def test_renames_only_provable_companion_sessions(self, tmp_path):
        from companion_backfill_source import backfill

        db = tmp_path / "state.db"
        _make_sessions_db(db, [
            ("n-companion-2C1F0B0E-0000-4000-8000-000000000001", "api_server"),
            ("api_1786900000_ab12cd", "api_server"),   # ambígua: NÃO tocar
            ("n-voice-2C1F0B0E-0000-4000-8000-000000000002-9f", "api_server"),
            ("api_1786900001_ef34gh", "cli"),          # nem é api_server
        ])

        changed = backfill(str(db), dry_run=False)

        conn = sqlite3.connect(db)
        got = dict(conn.execute("SELECT id, source FROM sessions").fetchall())
        conn.close()

        assert changed == 2
        assert got["n-companion-2C1F0B0E-0000-4000-8000-000000000001"] == "companion_ios"
        assert got["n-voice-2C1F0B0E-0000-4000-8000-000000000002-9f"] == "companion_ios"
        # A ambígua fica como está: um cliente de API não pode virar push.
        assert got["api_1786900000_ab12cd"] == "api_server"
        assert got["api_1786900001_ef34gh"] == "cli"

    def test_dry_run_changes_nothing(self, tmp_path):
        from companion_backfill_source import backfill

        db = tmp_path / "state.db"
        _make_sessions_db(db, [
            ("n-companion-2C1F0B0E-0000-4000-8000-000000000001", "api_server"),
        ])

        assert backfill(str(db), dry_run=True) == 1

        conn = sqlite3.connect(db)
        got = conn.execute("SELECT source FROM sessions").fetchone()[0]
        conn.close()
        assert got == "api_server"
