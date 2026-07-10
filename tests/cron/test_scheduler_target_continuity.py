"""Regression tests for universal continuable cron delivery.

The historical implementation only attached deliveries that matched the job's
creation origin.  Mike's workflow requires the stronger contract: when target
scope is configured, every successful user-facing delivery is seeded into the
actual delivery target so the next reply carries the cron/loop context.
"""

from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch


def _run_coro_now(coro, _loop):
    import asyncio

    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except BaseException as exc:  # noqa: BLE001 - preserve async failure in Future
        future.set_exception(exc)
    return future


def test_mirror_scope_defaults_to_origin():
    from cron.scheduler import _cron_mirror_delivery_scope

    assert _cron_mirror_delivery_scope({}) == "origin"
    assert _cron_mirror_delivery_scope({"cron": {}}) == "origin"


def test_mirror_scope_accepts_target():
    from cron.scheduler import _cron_mirror_delivery_scope

    assert _cron_mirror_delivery_scope(
        {"cron": {"mirror_delivery_scope": "target"}}
    ) == "target"


def test_target_scope_seeds_explicit_whatsapp_group_for_origin_user():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    target_chat = "target-group@g.us"
    origin_user = "owner@lid"

    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    cfg = MagicMock()
    cfg.platforms = {Platform.WHATSAPP: pconfig}

    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(success=True, raw_response={})
    adapter.create_handoff_thread.return_value = None
    adapter._session_store = MagicMock()

    loop = MagicMock()
    loop.is_running.return_value = True

    job = {
        "id": "target-continuity",
        "name": "Target continuity",
        "deliver": f"whatsapp:{target_chat}",
        "origin": {
            "platform": "whatsapp",
            "chat_id": "different-origin@lid",
            "user_id": origin_user,
        },
        "attach_to_session": True,
    }

    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {
                     "wrap_response": False,
                     "mirror_delivery": True,
                     "mirror_delivery_scope": "target",
                 }
             },
         ), \
         patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_run_coro_now), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        error = _deliver_result(
            job,
            "A decision is waiting for your reply.",
            adapters={Platform.WHATSAPP: adapter},
            loop=loop,
        )

    assert error is None
    adapter._session_store.get_or_create_session.assert_called_once()
    seeded = adapter._session_store.get_or_create_session.call_args.args[0]
    assert str(seeded.chat_id) == target_chat
    assert str(seeded.user_id) == origin_user
    assert seeded.chat_type == "group"
    mirror.assert_called_once()
    assert mirror.call_args.args[:2] == ("whatsapp", target_chat)
    assert mirror.call_args.kwargs["user_id"] == origin_user


def test_origin_scope_does_not_attach_explicit_other_target():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    cfg = MagicMock()
    cfg.platforms = {Platform.WHATSAPP: pconfig}

    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(success=True, raw_response={})
    adapter._session_store = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True

    job = {
        "id": "legacy-origin-scope",
        "name": "Legacy origin scope",
        "deliver": "whatsapp:other-group@g.us",
        "origin": {
            "platform": "whatsapp",
            "chat_id": "origin-group@g.us",
            "user_id": "owner@lid",
        },
        "attach_to_session": True,
    }

    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {
                     "wrap_response": False,
                     "mirror_delivery": True,
                     "mirror_delivery_scope": "origin",
                 }
             },
         ), \
         patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_run_coro_now), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        error = _deliver_result(
            job,
            "Legacy behavior remains isolated.",
            adapters={Platform.WHATSAPP: adapter},
            loop=loop,
        )

    assert error is None
    adapter._session_store.get_or_create_session.assert_not_called()
    mirror.assert_not_called()
