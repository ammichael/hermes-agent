"""Plugin de plataforma companion: backfill, devices, apns, watcher, adapter."""

import json as _json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from plugins.platforms.companion.apns import APNsSender
from plugins.platforms.companion.devices import DeviceStore
from plugins.platforms.companion.watcher import MessageWatcher, ensure_trigger


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


# Chave P-256 de teste. Não é a chave de produção e não abre nada:
# é gerada só para o JWT deste teste.
TEST_P8 = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgevZzL1gdAFr88hb2
OF/2NxApJCzGCEDdfSp6VQO30hyhRANCAAQRWz+jn65BtOMvdyHKcvjBeBSDZH2r
1RTwjmYSi9R/zpBnuQ4EiMnCqfMPWiZqB4QdbAd0E7oH50VpuZ1P087G
-----END PRIVATE KEY-----
"""


def _credentials_dir(tmp_path):
    directory = tmp_path / "companion"
    directory.mkdir()
    (directory / "apns.p8").write_text(TEST_P8)
    (directory / "apns.json").write_text(_json.dumps({"keyID": "ABCD123456", "teamID": "TEAM123456"}))
    return directory


class TestAPNsSender:
    def test_unavailable_without_credentials(self, tmp_path):
        assert APNsSender(tmp_path / "vazio").available() is False

    def test_available_with_credentials(self, tmp_path):
        assert APNsSender(_credentials_dir(tmp_path)).available() is True

    def test_send_builds_the_expected_headers_and_payload(self, tmp_path):
        captured = {}

        def fake_runner(argv, **kwargs):
            captured["argv"] = argv
            captured["config"] = kwargs.get("input") or ""
            # O corpo vai num arquivo referenciado por `data-binary = "@..."`.
            for line in captured["config"].splitlines():
                if line.startswith("data-binary"):
                    body_path = line.split("@", 1)[1].strip('"')
                    captured["body"] = _json.loads(open(body_path).read())
            headers_path = argv[argv.index("-D") + 1]
            open(headers_path, "w").write("HTTP/2 200\r\n\r\n")
            return subprocess.CompletedProcess(argv, 0)

        sender = APNsSender(_credentials_dir(tmp_path), runner=fake_runner)
        status = sender.send_alert(
            device_token="ab" * 32,
            environment="production",
            title="Aura",
            body="Saldo atualizado",
            session_id="api_1786900000_ab12cd",
            message_id="422755",
        )

        assert status == 200
        assert "--http2" in captured["argv"]
        config = captured["config"]
        assert 'header = "apns-topic: dev.meevi.n"' in config
        assert 'header = "apns-push-type: alert"' in config
        assert 'header = "apns-priority: 10"' in config
        assert 'header = "apns-collapse-id: n-msg-api_1786900000_ab12cd"' in config
        assert "api.push.apple.com" in config

        body = captured["body"]
        assert body["aps"]["alert"] == {"title": "Aura", "body": "Saldo atualizado"}
        assert body["aps"]["thread-id"] == "api_1786900000_ab12cd"
        assert body["kind"] == "message"
        assert body["session_id"] == "api_1786900000_ab12cd"
        assert body["message_id"] == "422755"
        # Sem NSE no app: mutable-content é bandeira sem consumidor.
        assert "mutable-content" not in body["aps"]
        assert "badge" not in body["aps"]

    def test_sandbox_environment_uses_the_sandbox_host(self, tmp_path):
        captured = {}

        def fake_runner(argv, **kwargs):
            captured["config"] = kwargs.get("input") or ""
            headers_path = argv[argv.index("-D") + 1]
            open(headers_path, "w").write("HTTP/2 200\r\n\r\n")
            return subprocess.CompletedProcess(argv, 0)

        APNsSender(_credentials_dir(tmp_path), runner=fake_runner).send_alert(
            device_token="ab" * 32, environment="sandbox",
            title="t", body="b", session_id="s", message_id="1",
        )
        assert "api.sandbox.push.apple.com" in captured["config"]

    def test_410_is_reported_so_the_caller_can_drop_the_device(self, tmp_path):
        def fake_runner(argv, **kwargs):
            headers_path = argv[argv.index("-D") + 1]
            open(headers_path, "w").write("HTTP/2 410\r\n\r\n")
            return subprocess.CompletedProcess(argv, 0)

        status = APNsSender(_credentials_dir(tmp_path), runner=fake_runner).send_alert(
            device_token="ab" * 32, environment="production",
            title="t", body="b", session_id="s", message_id="1",
        )
        assert status == 410

    def test_curl_failure_reports_zero_not_success(self, tmp_path):
        def fake_runner(argv, **kwargs):
            raise OSError("curl sumiu")

        status = APNsSender(_credentials_dir(tmp_path), runner=fake_runner).send_alert(
            device_token="ab" * 32, environment="production",
            title="t", body="b", session_id="s", message_id="1",
        )
        assert status == 0


def _make_state_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, display_name TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, timestamp REAL
        );
        CREATE TABLE gateway_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT, session_id TEXT, message_id INTEGER, timestamp REAL
        );
        """
    )
    ensure_trigger(conn)
    conn.commit()
    return conn


def _insert(conn, session_id, role, content):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,0)",
        (session_id, role, content),
    )
    conn.commit()


class TestMessageWatcher:
    def test_only_assistant_rows_in_companion_sessions_are_pending(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.execute("INSERT INTO sessions VALUES ('s-api', 'api_server', 'Robô')")
        conn.commit()

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()

        _insert(conn, "s-app", "assistant", "Saldo atualizado")
        _insert(conn, "s-app", "user", "e o saldo?")       # eco: não notifica
        _insert(conn, "s-api", "assistant", "resposta de API")  # outra origem

        pending = watcher.pending()

        assert [p.session_id for p in pending] == ["s-app"]
        assert pending[0].preview == "Saldo atualizado"
        assert pending[0].title == "Aura"

    def test_bootstrap_does_not_replay_history(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.commit()
        for i in range(5):
            _insert(conn, "s-app", "assistant", f"antiga {i}")

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()

        assert watcher.pending() == []

    def test_commit_advances_the_cursor_and_survives_a_new_instance(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.commit()

        cursor_path = tmp_path / "cursor.json"
        watcher = MessageWatcher(str(db), cursor_path)
        watcher.bootstrap()
        _insert(conn, "s-app", "assistant", "primeira")
        _insert(conn, "s-app", "assistant", "segunda")

        pending = watcher.pending()
        assert len(pending) == 2

        watcher.commit(pending[0].event_id)

        # Só a segunda continua pendente, e isso sobrevive a um processo novo.
        assert [p.preview for p in MessageWatcher(str(db), cursor_path).pending()] == ["segunda"]

    def test_pending_alone_does_not_advance_the_cursor(self, tmp_path):
        """Push perdido não tem conserto; push repetido tem (collapse-id).
        Por isso o cursor só anda depois de a APNs aceitar."""
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.commit()

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()
        _insert(conn, "s-app", "assistant", "uma só")

        assert len(watcher.pending()) == 1
        assert len(watcher.pending()) == 1  # sem commit, continua pendente

    def test_ensure_trigger_is_idempotent_and_recreates_when_missing(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("DROP TRIGGER gateway_event_message_created")
        conn.commit()

        ensure_trigger(conn)
        ensure_trigger(conn)  # duas vezes não pode explodir

        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.commit()
        _insert(conn, "s-app", "assistant", "depois de recriar")

        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_events WHERE type='message.created'"
        ).fetchone()[0]
        assert count == 1

    def test_preview_is_truncated(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura')")
        conn.commit()

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()
        _insert(conn, "s-app", "assistant", "x" * 500)

        assert len(watcher.pending()[0].preview) <= 180
