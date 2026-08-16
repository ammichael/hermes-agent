"""Plugin de plataforma companion: backfill, devices, apns, watcher, adapter."""

import asyncio
import json as _json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from plugins.platforms.companion.adapter import CompanionAdapter
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
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, display_name TEXT);
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
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
        conn.execute("INSERT INTO sessions VALUES ('s-api', 'api_server', 'Robô', NULL)")
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
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
        conn.commit()
        for i in range(5):
            _insert(conn, "s-app", "assistant", f"antiga {i}")

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()

        assert watcher.pending() == []

    def test_commit_advances_the_cursor_and_survives_a_new_instance(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
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
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
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

        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
        conn.commit()
        _insert(conn, "s-app", "assistant", "depois de recriar")

        count = conn.execute(
            "SELECT COUNT(*) FROM gateway_events WHERE type='message.created'"
        ).fetchone()[0]
        assert count == 1

    def test_preview_is_truncated(self, tmp_path):
        db = tmp_path / "state.db"
        conn = _make_state_db(db)
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
        conn.commit()

        watcher = MessageWatcher(str(db), tmp_path / "cursor.json")
        watcher.bootstrap()
        _insert(conn, "s-app", "assistant", "x" * 500)

        assert len(watcher.pending()[0].preview) <= 180


# ---------------------------------------------------------------- adapter
#
# `dispatch_http_event` é async, e este venv NÃO tem `pytest-asyncio`
# instalado — um `async def test_...` marcado com `@pytest.mark.asyncio` é
# coletado, avisa "Unknown mark" e FALHA sem executar corpo nenhum
# (confirmado em tests/test_model_tools_async_bridge.py). Por isso os testes
# do adapter são síncronos e atravessam a fronteira async com `asyncio.run`.


class _Config:
    """PlatformConfig mínimo: o adapter só lê `extra`."""

    def __init__(self, **extra):
        self.extra = extra
        self.enabled = True


def _adapter(tmp_path, *, approved=("iphone-do-mike",), sender=None):
    class FakePairing:
        def is_approved(self, platform, user_id):
            return user_id in approved

        def generate_code(self, platform, user_id, user_name=""):
            return "ABCD1234"

    adapter = CompanionAdapter(_Config(
        companion_dir=str(tmp_path / "companion"),
        state_db=str(tmp_path / "state.db"),
    ))
    adapter._pairing = FakePairing()
    if sender is not None:
        adapter._apns = sender
    return adapter


def _dispatch(adapter, payload):
    return asyncio.run(adapter.dispatch_http_event(payload))


def _verify(adapter, header):
    return asyncio.run(adapter.verify_http_event_request(header))


def _request(adapter, header, payload):
    """Uma requisicao inteira: verify e dispatch na MESMA task.

    E assim que o `api_server` faz (`api_server.py:1909` e `:1946`), e e a
    unica forma de exercitar a ponte entre os dois. Rodar cada um em seu
    proprio `asyncio.run` daria contextos diferentes e o dispatch nunca veria
    o aparelho autenticado — que e exatamente o bug que o ContextVar conserta.
    """
    async def _run():
        ok, code = await adapter.verify_http_event_request(header)
        if not ok:
            return {"ok": False, "error": code}
        return await adapter.dispatch_http_event(payload)

    return asyncio.run(_run())


class TestCompanionControlPlane:
    def test_pair_request_returns_a_code(self, tmp_path):
        adapter = _adapter(tmp_path)
        result = _dispatch(adapter, {
            "type": "pair.request",
            "device_id": "iphone-do-mike",
            "device_name": "iPhone do Mike",
        })
        assert result["status"] == "pending"
        assert result["code"] == "ABCD1234"

    def test_pair_claim_issues_once(self, tmp_path):
        adapter = _adapter(tmp_path)
        first = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})
        assert first["status"] == "issued"
        assert first["token"]

        second = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})
        assert second["status"] == "already_claimed"
        assert "token" not in second

    def test_pair_claim_without_approval_issues_nothing(self, tmp_path):
        adapter = _adapter(tmp_path, approved=())
        result = _dispatch(
            adapter, {"type": "pair.claim", "device_id": "iphone-nao-aprovado"}
        )
        assert result["status"] == "pending"
        assert "token" not in result

    def test_push_register_requires_a_paired_device(self, tmp_path):
        adapter = _adapter(tmp_path)
        issued = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})

        ok = _request(adapter, f"Bearer {issued['token']}", {
            "type": "push.register",
            "apns_token": "ab" * 32,
            "environment": "production",
        })
        assert ok["ok"] is True
        assert adapter._devices.push_targets() == [
            ("iphone-do-mike", "ab" * 32, "production")
        ]

    def test_push_register_without_credential_registers_nothing(self, tmp_path):
        """O anônimo alcança o dispatch (o enrolamento precisa disso), então
        quem barra `push.register` sem credencial é o dispatch."""
        adapter = _adapter(tmp_path)
        _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})
        _verify(adapter, "")

        result = _dispatch(adapter, {
            "type": "push.register",
            "apns_token": "ab" * 32,
            "environment": "production",
        })
        assert result["ok"] is False
        assert result["error"] == "unauthenticated"
        assert adapter._devices.push_targets() == []

    def test_unknown_type_is_refused(self, tmp_path):
        adapter = _adapter(tmp_path)
        result = _dispatch(adapter, {"type": "delete.everything"})
        assert result["ok"] is False
        assert result["error"] == "unknown_type"


class TestCompanionVerifier:
    def test_verifier_accepts_the_issued_token(self, tmp_path):
        adapter = _adapter(tmp_path)
        issued = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})
        ok, _code = _verify(adapter, f"Bearer {issued['token']}")
        assert ok is True

    def test_verifier_refuses_a_revoked_device(self, tmp_path):
        """Revogação é imediata porque `is_approved` é conferido a cada evento."""
        revoked = set()

        adapter = _adapter(tmp_path)
        issued = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})

        class RevokingPairing:
            def is_approved(self, platform, user_id):
                return user_id not in revoked

            def generate_code(self, platform, user_id, user_name=""):
                return "ABCD1234"

        adapter._pairing = RevokingPairing()
        assert _verify(adapter, f"Bearer {issued['token']}")[0] is True

        revoked.add("iphone-do-mike")
        ok, code = _verify(adapter, f"Bearer {issued['token']}")
        assert ok is False
        assert code == "device_not_approved"

    def test_verifier_refuses_a_presented_credential_that_is_garbage(self, tmp_path):
        adapter = _adapter(tmp_path)
        for header in ["Bearer ", "Bearer nao-existe", "Basic abc"]:
            ok, _ = _verify(adapter, header)
            assert ok is False, header

    def test_absent_header_is_anonymous_not_refused(self, tmp_path):
        """A rota roda o verificador ANTES de olhar o corpo
        (`gateway/platforms/api_server.py:1907-1928`), e um `False` vira 401
        antes de o `type` ser lido. Recusar o pedido sem cabeçalho tornaria
        `pair.request`/`pair.claim` inalcançáveis — o telefone ainda não tem a
        credencial que estaria sendo exigida. Anônimo entra; o dispatch é quem
        decide o que o anônimo pode."""
        adapter = _adapter(tmp_path)
        ok, code = _verify(adapter, "")
        assert ok is True
        assert code == ""

    def test_pair_types_are_reachable_without_a_token(self, tmp_path):
        """`pair.request` e `pair.claim` são o enrolamento: eles não podem
        exigir a credencial que ainda não existe."""
        adapter = _adapter(tmp_path)
        assert adapter.requires_credential("pair.request") is False
        assert adapter.requires_credential("pair.claim") is False
        assert adapter.requires_credential("push.register") is True


class TestCompanionPushDrain:
    def _state_db(self, tmp_path):
        conn = _make_state_db(tmp_path / "state.db")
        conn.execute("INSERT INTO sessions VALUES ('s-app', 'companion_ios', 'Aura', NULL)")
        conn.commit()
        return conn

    def test_drain_sends_and_advances_the_cursor(self, tmp_path):
        conn = self._state_db(tmp_path)
        sent = []

        class FakeSender:
            def available(self):
                return True

            def send_alert(self, **kwargs):
                sent.append(kwargs)
                return 200

        adapter = _adapter(tmp_path, sender=FakeSender())
        adapter._devices.claim("iphone-do-mike", approved=True)
        adapter._devices.register_push("iphone-do-mike", "ab" * 32, "production")
        adapter._watcher.bootstrap()

        _insert(conn, "s-app", "assistant", "Saldo atualizado")
        adapter._drain_once()

        assert [s["body"] for s in sent] == ["Saldo atualizado"]
        assert sent[0]["title"] == "Aura"
        assert sent[0]["session_id"] == "s-app"
        assert adapter._watcher.pending() == []

    def test_dead_token_is_dropped_and_the_cursor_still_moves(self, tmp_path):
        conn = self._state_db(tmp_path)

        class GoneSender:
            def available(self):
                return True

            def send_alert(self, **kwargs):
                return 410

        adapter = _adapter(tmp_path, sender=GoneSender())
        adapter._devices.claim("iphone-do-mike", approved=True)
        adapter._devices.register_push("iphone-do-mike", "ab" * 32, "production")
        adapter._watcher.bootstrap()

        _insert(conn, "s-app", "assistant", "some")
        adapter._drain_once()

        assert adapter._devices.push_targets() == []
        assert adapter._watcher.pending() == []

    def test_a_refused_push_holds_the_cursor_for_the_next_poll(self, tmp_path):
        conn = self._state_db(tmp_path)

        class FailingSender:
            def available(self):
                return True

            def send_alert(self, **kwargs):
                return 500

        adapter = _adapter(tmp_path, sender=FailingSender())
        adapter._devices.claim("iphone-do-mike", approved=True)
        adapter._devices.register_push("iphone-do-mike", "ab" * 32, "production")
        adapter._watcher.bootstrap()

        _insert(conn, "s-app", "assistant", "tenta de novo")
        adapter._drain_once()

        assert [p.preview for p in adapter._watcher.pending()] == ["tenta de novo"]


class TestCompanionOutbound:
    def test_image_becomes_a_media_line_na_conversa(self, tmp_path):
        """O default da base (`base.py:4707`) manda "Couldn't deliver the image
        attachment" e apaga o caminho. Aqui a linha `MEDIA:` tem de sobreviver:
        é ela que o histórico resolve para data URL."""
        adapter = _adapter(tmp_path)
        written = []
        adapter._append_to_session = (
            lambda chat_id, content: written.append((chat_id, content)) or "412"
        )

        image = tmp_path / "grafico.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")

        result = asyncio.run(
            adapter.send_image_file("s-app", str(image), caption="Segue o gráfico")
        )

        assert result.success is True
        assert result.message_id == "412"
        chat_id, content = written[0]
        assert chat_id == "s-app"
        assert content.startswith("Segue o gráfico\nMEDIA:")
        assert content.endswith("grafico.png")

    def test_unsafe_media_path_is_refused(self, tmp_path):
        adapter = _adapter(tmp_path)
        result = asyncio.run(
            adapter.send_image_file("s-app", str(tmp_path / "nao-existe.png"))
        )
        assert result.success is False
        assert result.error == "unsafe_media_path"

    def test_send_to_an_unknown_session_fails_loudly(self, tmp_path):
        adapter = _adapter(tmp_path)
        adapter._append_to_session = lambda chat_id, content: None

        result = asyncio.run(adapter.send("s-inexistente", "oi"))

        assert result.success is False
        assert result.error == "unknown_session"


class TestCompanionAuthIsolation:
    """A credencial pertence à requisição, nunca ao adapter.

    O `api_server` chama `verify_http_event_request` e depois
    `dispatch_http_event` sem nenhum parâmetro ligando um ao outro, e o aiohttp
    atende requisições concorrentes no mesmo event loop. Guardar o aparelho
    autenticado num atributo da instância fazia um anônimo que entrasse entre o
    verify e o dispatch de um aparelho pareado herdar a credencial dele.
    """

    def test_anonymous_request_interleaved_with_an_authenticated_one_stays_anonymous(
        self, tmp_path
    ):
        adapter = _adapter(tmp_path)
        issued = _dispatch(adapter, {"type": "pair.claim", "device_id": "iphone-do-mike"})
        token = issued["token"]

        async def scenario():
            barrier = asyncio.Event()

            async def paired():
                ok, _ = await adapter.verify_http_event_request(f"Bearer {token}")
                assert ok is True
                # O anônimo roda inteiro entre o verify e o dispatch deste.
                barrier.set()
                await asyncio.sleep(0.05)
                return await adapter.dispatch_http_event({
                    "type": "push.register",
                    "apns_token": "ab" * 32,
                    "environment": "production",
                })

            async def anonymous():
                await barrier.wait()
                ok, _ = await adapter.verify_http_event_request("")
                assert ok is True  # anônimo entra; o dispatch é quem barra
                return await adapter.dispatch_http_event({
                    "type": "push.register",
                    "apns_token": "cd" * 32,
                    "environment": "production",
                })

            return await asyncio.gather(paired(), anonymous())

        paired_result, anonymous_result = asyncio.run(scenario())

        # O anônimo nao pode ter registrado nada...
        assert anonymous_result["ok"] is False
        assert anonymous_result["error"] == "unauthenticated"
        # ...e o pareado nao pode ter perdido a credencial dele no caminho.
        assert paired_result["ok"] is True
        assert adapter._devices.push_targets() == [
            ("iphone-do-mike", "ab" * 32, "production")
        ]


class TestSinglePoller:
    """O gateway sobe a plataforma uma vez por profile — cinco nesta máquina —
    e nenhuma instância é ciente de profile. Sem trava, uma mensagem vira cinco
    pushes e o cursor sofre corrida."""

    def test_only_the_first_instance_gets_the_poll_lock(self, tmp_path):
        first = _adapter(tmp_path)
        second = _adapter(tmp_path)

        assert first._acquire_poll_lock() is True
        assert second._acquire_poll_lock() is False

        # Quem solta devolve a vez, senão um restart parcial deixaria o push
        # morto até o processo inteiro cair.
        first._release_poll_lock()
        assert second._acquire_poll_lock() is True
        second._release_poll_lock()
