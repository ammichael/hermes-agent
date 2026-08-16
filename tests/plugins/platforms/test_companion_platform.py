"""Plugin de plataforma companion: backfill, devices, apns, watcher, adapter."""

import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from plugins.platforms.companion.devices import DeviceStore


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


class TestDeviceStore:
    def test_claim_issues_once_then_reports_already_claimed(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")

        state, token = store.claim("iphone-do-mike", approved=True)
        assert state == "issued"
        assert token and len(token) >= 32

        state2, token2 = store.claim("iphone-do-mike", approved=True)
        assert state2 == "already_claimed"
        assert token2 is None

    def test_claim_without_approval_issues_nothing(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        state, token = store.claim("iphone-do-mike", approved=False)
        assert state == "pending"
        assert token is None
        assert store.verify("qualquer") is None

    def test_verify_returns_device_id_for_the_issued_token(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        _, token = store.claim("iphone-do-mike", approved=True)
        assert store.verify(token) == "iphone-do-mike"
        assert store.verify(token + "x") is None
        assert store.verify("") is None

    def test_token_is_not_stored_in_plaintext(self, tmp_path):
        path = tmp_path / "devices.json"
        store = DeviceStore(path)
        _, token = store.claim("iphone-do-mike", approved=True)
        assert token not in path.read_text()

    def test_file_is_private_even_when_it_already_existed_loose(self, tmp_path):
        path = tmp_path / "devices.json"
        path.write_text("{}")
        os.chmod(path, 0o644)

        store = DeviceStore(path)
        store.claim("iphone-do-mike", approved=True)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_register_push_and_targets(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.claim("iphone-do-mike", approved=True)

        assert store.register_push("iphone-do-mike", "ab" * 32, "production") is True
        assert store.push_targets() == [("iphone-do-mike", "ab" * 32, "production")]

        # Aparelho desconhecido não entra no registro por um POST.
        assert store.register_push("aparelho-fantasma", "cd" * 32, "sandbox") is False

    def test_register_push_rejects_non_hex_token(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.claim("iphone-do-mike", approved=True)
        assert store.register_push("iphone-do-mike", "não-é-hex", "production") is False

    def test_drop_removes_the_device_from_targets(self, tmp_path):
        store = DeviceStore(tmp_path / "devices.json")
        store.claim("iphone-do-mike", approved=True)
        store.register_push("iphone-do-mike", "ab" * 32, "production")

        store.drop("iphone-do-mike")

        assert store.push_targets() == []

    def test_state_survives_a_new_instance(self, tmp_path):
        path = tmp_path / "devices.json"
        _, token = DeviceStore(path).claim("iphone-do-mike", approved=True)
        assert DeviceStore(path).verify(token) == "iphone-do-mike"
