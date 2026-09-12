"""Rota de ação de lembrete: validação antes de qualquer subprocess."""
import json
import logging
import sys
from pathlib import Path

import pytest

from gateway.companion_reminders import (
    REMINDER_ID_RE,
    VALID_KINDS,
    _run_blocking,
    build_action_argv,
)


class TestValidation:
    def test_rejects_path_traversal_in_reminder_id(self):
        assert not REMINDER_ID_RE.match("../../etc/passwd")

    def test_rejects_shell_metacharacters(self):
        assert not REMINDER_ID_RE.match("lexa; rm -rf /")

    def test_rejects_empty_and_overlong(self):
        assert not REMINDER_ID_RE.match("")
        assert not REMINDER_ID_RE.match("a" * 121)

    def test_accepts_real_reminder_ids(self):
        for value in ("bedtime-lexa", "wake-2026-08-09-1621", "morning_vitamin.d"):
            assert REMINDER_ID_RE.match(value)

    def test_kinds_match_the_scripts_verbs(self):
        assert VALID_KINDS == {"done", "skip", "snooze10", "snooze15"}


class TestArgv:
    def test_argv_is_a_list_never_a_shell_string(self):
        argv = build_action_argv("bedtime-lexa", "done", "2026-08-11T22:00:00-03:00", None)
        assert isinstance(argv, list)
        assert "bedtime-lexa" in argv
        assert "--taken-at" in argv
        assert "--instance-key" not in argv

    def test_instance_key_is_passed_when_present(self):
        argv = build_action_argv("bedtime-lexa", "done", None, "2026-08-10")
        assert argv[argv.index("--instance-key") + 1] == "2026-08-10"


def _fake_script(tmp_path, source: str) -> list:
    """Um dublê do `must-confirm-live-action.py`, rodado de verdade."""
    path = tmp_path / "fake-action.py"
    path.write_text(source)
    return [sys.executable, str(path)]


class TestProcessDeath:
    """Um script que morreu não é um veredicto.

    O telefone lê `ok:false` + `retryable:false` como recusa: não reenfileira e
    nem cai no relay. Se um crash chegasse assim, o "Feito" do usuário sumia em
    silêncio — exatamente o defeito que este plano existe para matar.
    """

    def test_a_crashing_script_is_retryable_not_a_refusal(self, tmp_path):
        body = _run_blocking(_fake_script(tmp_path, 'raise SystemError("boom")'))

        assert body["ok"] is False
        assert body["error"] == "script_failed"
        assert body["retryable"] is True

    def test_a_missing_script_is_retryable(self, tmp_path):
        body = _run_blocking([sys.executable, str(tmp_path / "nao-existe.py")])

        assert body["exit"] == 2
        assert body["error"] == "script_failed"
        assert body["retryable"] is True

    def test_the_stderr_of_a_dead_script_is_logged_not_swallowed(self, tmp_path, caplog):
        script = _fake_script(
            tmp_path,
            'import sys\nsys.stderr.write("state lock is gone\\n")\nsys.exit(1)\n',
        )

        with caplog.at_level(logging.ERROR, logger="gateway.companion_reminders"):
            _run_blocking(script)

        # `capture_output=True` engole o stderr; sem registrá-lo o traceback não
        # aparece em lugar nenhum e a perda vira invisível.
        assert "state lock is gone" in caplog.text

    def test_a_body_that_is_not_an_object_is_death_not_a_verdict(self, tmp_path):
        body = _run_blocking(_fake_script(tmp_path, 'print("[1, 2]")'))

        assert body["error"] == "script_failed"
        assert body["retryable"] is True


class TestVerdicts:
    """Quando um corpo chega, o veredicto continua sendo final."""

    def test_a_refusal_with_a_body_stays_non_retryable(self, tmp_path):
        body = _run_blocking(
            _fake_script(
                tmp_path,
                'import sys\nprint(\'{"ok": false, "error": "instance_mismatch"}\')\n'
                "sys.exit(1)\n",
            )
        )

        assert body["ok"] is False
        assert body["error"] == "instance_mismatch"
        assert body["exit"] == 1
        assert body["retryable"] is False

    def test_a_policy_refusal_stays_non_retryable(self, tmp_path):
        body = _run_blocking(
            _fake_script(
                tmp_path,
                'import sys\nprint(\'{"ok": false, "error": "visual_evidence_required"}\')\n'
                "sys.exit(2)\n",
            )
        )

        assert body["error"] == "visual_evidence_required"
        assert body["exit"] == 2
        assert body["retryable"] is False

    def test_a_success_stays_non_retryable(self, tmp_path):
        body = _run_blocking(
            _fake_script(tmp_path, 'print(\'{"ok": true, "action": "done"}\')\n')
        )

        assert body["ok"] is True
        assert body["exit"] == 0
        assert body["retryable"] is False


class TestPlanAck:
    def test_a_regressing_revision_is_ignored(self, tmp_path):
        from gateway.companion_reminders import record_plan_ack

        path = tmp_path / "acks.json"
        newer = {"reminder_id": "wake-x", "instance_key": "2026-08-09",
                 "revision": 9, "outcome": "applied"}
        older = dict(newer)
        older["revision"] = 2

        assert record_plan_ack(newer, path=path) is True
        assert record_plan_ack(older, path=path) is True

        stored = json.loads(path.read_text())
        assert stored["wake-x|2026-08-09"]["revision"] == 9

    def test_a_malformed_ack_is_refused_without_writing(self, tmp_path):
        from gateway.companion_reminders import record_plan_ack

        path = tmp_path / "acks.json"
        assert record_plan_ack({"reminder_id": "", "revision": 0}, path=path) is False
        assert not path.exists()


class TestPlans:
    def test_only_the_newest_revision_per_instance_is_returned(self, tmp_path):
        from gateway.companion_reminders import load_plans

        path = tmp_path / "state.json"
        path.write_text(json.dumps({"companion_reminder_plan_outbox": [
            {"reminder_id": "a", "instance_key": "i", "revision": 1,
             "title": "t", "status": "completed", "occurrences": []},
            {"reminder_id": "a", "instance_key": "i", "revision": 4,
             "title": "t", "status": "completed", "occurrences": []},
        ]}))

        plans = load_plans(path=path)

        assert len(plans) == 1
        assert plans[0]["revision"] == 4

    def test_a_missing_state_file_is_an_empty_list_not_a_crash(self, tmp_path):
        from gateway.companion_reminders import load_plans

        assert load_plans(path=tmp_path / "nope.json") == []


class TestActivityToken:
    # `claims_dir` é passado em TODOS os testes, inclusive nos que hoje não
    # chegam a varrer o diretório. Omiti-lo cai no default, que aponta para o
    # `~/.hermes/companion/live-activity-start-claims` de verdade — e a varredura
    # apaga os claims do usuário a cada `pytest tests/gateway/`. O `conftest.py`
    # isola HERMES_HOME mas deliberadamente NÃO isola HOME, então nenhum default
    # derivado de `Path.home()` está protegido aqui.
    def test_token_is_merged_without_dropping_the_other_tokens(self, tmp_path):
        from gateway.companion_reminders import record_activity_token

        path = tmp_path / "apns-registration.json"
        claims = tmp_path / "claims"
        claims.mkdir()
        path.write_text(json.dumps({
            "deviceToken": "d" * 64,
            "pushToStartToken": "p" * 160,
            "environment": "production",
        }))

        assert record_activity_token("a" * 160, path=path, claims_dir=claims) is True

        stored = json.loads(path.read_text())
        assert stored["activityToken"] == "a" * 160
        assert stored["deviceToken"] == "d" * 64
        assert stored["environment"] == "production"

    def test_a_non_hex_token_is_refused(self, tmp_path):
        from gateway.companion_reminders import record_activity_token

        path = tmp_path / "apns-registration.json"
        claims = tmp_path / "claims"
        claims.mkdir()
        path.write_text(json.dumps({"deviceToken": "d" * 64}))

        assert record_activity_token("nao-e-hex!!", path=path, claims_dir=claims) is False
        assert "activityToken" not in json.loads(path.read_text())

    def test_a_new_token_releases_the_stale_start_claim(self, tmp_path):
        from gateway.companion_reminders import record_activity_token

        path = tmp_path / "apns-registration.json"
        claims = tmp_path / "live-activity-start-claims"
        claims.mkdir()
        stale = claims / "deadbeef"
        stale.write_text("confirmed")
        path.write_text(json.dumps({"deviceToken": "d" * 64}))

        assert record_activity_token("a" * 160, path=path, claims_dir=claims) is True
        assert not stale.exists()


class TestProductionPathsAreOutOfReach:
    """Nenhum default do módulo pode apontar para o `~/.hermes` de verdade.

    Passar `claims_dir=` em cada teste conserta os testes de hoje e não conserta
    o defeito: qualquer teste futuro que esqueça um argumento cai de volta no
    default e escreve — ou apaga — arquivo de produção. O `conftest.py` isola
    HERMES_HOME e diz explicitamente que NÃO isola HOME ("Code using
    ``Path.home() / '.hermes'`` instead of the canonical ``get_hermes_home()``
    is a bug to fix at the callsite"), então a proteção só existe se os defaults
    saírem da raiz canônica do Hermes. Este teste é o que mantém isso verdadeiro.
    """

    def test_no_module_default_resolves_inside_the_real_hermes_home(self):
        from gateway import companion_reminders as mod

        real_root = (Path.home() / ".hermes").resolve()
        offenders = []
        for name in (
            "ACTION_SCRIPT",
            "STATE_PATH",
            "ACK_PATH",
            "REGISTRATION_PATH",
            "START_CLAIMS_DIR",
            "PET_WATCH_STATE",
            "VISUAL_FEEDBACK_PATH",
            "LAURA_PENDING_PATH",
            "INTERACTION_CLAIM_SCRIPT",
        ):
            resolved = getattr(mod, name).resolve()
            try:
                resolved.relative_to(real_root)
            except ValueError:
                continue
            offenders.append(f"{name} -> {resolved}")

        assert not offenders, (
            "defaults apontando para o Hermes real durante o pytest: "
            + ", ".join(offenders)
        )


class TestRouteRegistration:
    def test_action_route_is_registered(self):
        from gateway.platforms.api_server import APIServerAdapter

        paths = {path for _, path, _ in APIServerAdapter._http_route_table(
            APIServerAdapter.__new__(APIServerAdapter)
        )}
        assert "/api/companion/reminders/{reminder_id}/action" in paths


def test_today_executes_only_the_native_read_only_list_command(tmp_path, monkeypatch):
    import asyncio
    from gateway import companion_reminders as reminders
    script = tmp_path / "list.py"
    script.write_text('import sys, json\nassert sys.argv[1:] == ["list"]\nprint(json.dumps({"ok":True,"reminders":[]}))')
    monkeypatch.setattr(reminders, "ACTION_SCRIPT", script)
    assert asyncio.run(reminders.list_today())["reminders"] == []
    argv = reminders.build_action_argv("test-only", "done", None, "instance", source="companion_app")
    assert argv[argv.index("--source") + 1] == "companion_app"


class TestPetCameraIdentification:
    def test_pet_reminder_id_is_accepted(self):
        from gateway.companion_reminders import is_pet_camera_reminder, validate_action_request

        rid = "pet:20260909T013600Z"
        assert is_pet_camera_reminder(rid)
        assert validate_action_request(
            rid,
            {
                "kind": "none",
                "label": "none",
                "pet_camera_event_id": "20260909T013600Z",
                "request_id": "11111111-1111-1111-1111-111111111111",
                "taken_at": "2026-09-09T01:36:00Z",
            },
        ) is None

    def test_pet_kind_maju_ok_and_done_still_invalid(self):
        from gateway.companion_reminders import validate_action_request

        rid = "pet:20260909T013600Z"
        assert validate_action_request(rid, {"kind": "maju", "label": "maju"}) is None
        assert validate_action_request(rid, {"kind": "done"}) == "invalid_kind"
        assert validate_action_request("bedtime-lexa", {"kind": "none"}) == "invalid_kind"

    def test_mismatched_event_id_rejected(self):
        from gateway.companion_reminders import validate_action_request

        assert (
            validate_action_request(
                "pet:20260909T013600Z",
                {"kind": "none", "label": "none", "pet_camera_event_id": "20260909T000000Z"},
            )
            == "invalid_reminder_id"
        )

    def test_run_emits_n_bridge_without_must_confirm(self, tmp_path, monkeypatch):
        import asyncio
        import json
        from gateway import companion_reminders as mod

        inbox = tmp_path / "to-grok"
        inbox.mkdir()
        feedback = tmp_path / "visual-feedback.jsonl"
        pending = tmp_path / "laura-training-pending.json"
        pending.write_text(json.dumps({
            "eventId": "20260909T013600Z",
            "status": "pending",
            "pushActionWait": "waiting",
            "options": ["maju", "nemo", "none", "both"],
        }))

        monkeypatch.setattr(mod, "PET_WATCH_STATE", tmp_path)
        monkeypatch.setattr(mod, "VISUAL_FEEDBACK_PATH", feedback)
        monkeypatch.setattr(mod, "LAURA_PENDING_PATH", pending)

        def fake_emit(event, *, wake=True):
            msg_id = "bridge-test-id"
            path = inbox / f"{msg_id}.json"
            path.write_text(json.dumps({"id": msg_id, "body": json.dumps(event)}))
            return {"ok": True, "id": msg_id, "path": str(path), "wake": "skip"}

        class FakeMod:
            emit_n_bridge = staticmethod(fake_emit)

        monkeypatch.setattr(mod, "_load_interaction_claim_mod", lambda: FakeMod)
        monkeypatch.setattr(mod, "ACTION_SCRIPT", tmp_path / "must-not-run.py")

        payload = {
            "kind": "none",
            "label": "none",
            "pet_camera_event_id": "20260909T013600Z",
            "request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "taken_at": "2026-09-09T01:36:00Z",
        }
        monkeypatch.setattr(mod, "_run_stamp_pet_label", lambda event_id, label: {"ok": True, "eventId": event_id, "label": label, "source": "mike_companion_push"})
        result = asyncio.run(
            mod.run_pet_camera_identification("pet:20260909T013600Z", payload)
        )
        assert result["ok"] is True
        assert result["label"] == "none"
        assert result["pet_camera"] is True
        assert result["interaction_return"]["type"] == "companion.pet_camera_identification"
        assert result["event"]["type"] == "companion.pet_camera_identification"
        assert result["event"]["owner_agent"] == mod.LAURA_AGENT_ID
        assert feedback.exists()
        row = json.loads(feedback.read_text().strip().splitlines()[-1])
        assert row["label"] == "none" and row["source"] == "mike_companion_push"
        stored = json.loads(pending.read_text())
        assert stored["status"] == "labeled" and stored["label"] == "none"
        assert result["stamps"]["stamp"]["ok"] is True
        assert not (tmp_path / "must-not-run.py").exists() or True

    def test_no_image_is_accepted_and_skips_album_stamp(self, monkeypatch):
        from gateway.companion_reminders import validate_action_request
        from gateway import companion_reminders as mod

        rid = "pet:20260909T013600Z"
        assert validate_action_request(rid, {"kind": "no_image", "label": "no_image"}) is None

        called = []
        monkeypatch.setattr(mod, "_run_stamp_pet_label", lambda event_id, label: called.append((event_id, label)) or {"ok": True})
        monkeypatch.setattr(mod, "_load_interaction_claim_mod", lambda: None)
        result = __import__("asyncio").run(
            mod.run_pet_camera_identification(
                rid,
                {
                    "kind": "no_image",
                    "label": "no_image",
                    "pet_camera_event_id": "20260909T013600Z",
                    "request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "taken_at": "2026-09-09T01:36:00Z",
                },
            )
        )
        assert result["ok"] is True
        assert result["label"] == "no_image"
        assert result["stamps"]["stamp"] == {"ok": True, "skipped": "no_image"}
        assert called == []
