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


def test_target_scope_seeds_explicit_whatsapp_cloud_group_for_origin_user():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    target_chat = "target-group@g.us"
    origin_user = "owner@lid"

    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    cfg = MagicMock()
    cfg.platforms = {Platform.WHATSAPP_CLOUD: pconfig}

    class _CloudGroupAdapter(MagicMock):
        async def get_chat_info(self, chat_id):
            # The real Cloud adapter cannot query groups and reports DM.
            return {"chat_id": chat_id, "type": "dm"}

    adapter = _CloudGroupAdapter()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, raw_response={}))
    adapter.create_handoff_thread = AsyncMock(return_value=None)
    adapter._session_store = MagicMock()

    loop = MagicMock()
    loop.is_running.return_value = True

    job = {
        "id": "target-continuity",
        "name": "Target continuity",
        "deliver": f"whatsapp_cloud:{target_chat}",
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
            adapters={Platform.WHATSAPP_CLOUD: adapter},
            loop=loop,
        )

    assert error is None
    adapter._session_store.get_or_create_session.assert_called_once()
    seeded = adapter._session_store.get_or_create_session.call_args.args[0]
    assert str(seeded.chat_id) == target_chat
    assert str(seeded.user_id) == origin_user
    assert seeded.chat_type == "group"
    mirror.assert_called_once()
    assert mirror.call_args.args[:2] == ("whatsapp_cloud", target_chat)
    assert mirror.call_args.kwargs["user_id"] == origin_user


def test_origin_scope_does_not_attach_explicit_other_target():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    pconfig.extra = {}
    cfg = MagicMock()
    cfg.platforms = {Platform.WHATSAPP: pconfig}

    class _NoProbeAdapter(MagicMock):
        async def get_chat_info(self, chat_id):
            raise AssertionError(f"unexpected chat-info probe for {chat_id}")

    adapter = _NoProbeAdapter()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, raw_response={}))
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


def test_target_scope_seeds_cold_session_on_standalone_delivery(tmp_path):
    """Standalone fallback must create the destination before mirroring it."""
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    target_chat = "cold-target@g.us"
    origin_user = "owner@lid"
    pconfig = MagicMock(enabled=True, extra={})
    cfg = MagicMock(platforms={Platform.WHATSAPP: pconfig})
    cfg.sessions_dir = tmp_path / "sessions"
    session_store = MagicMock(spec=["get_or_create_session"])

    job = {
        "id": "cold-standalone-target",
        "name": "Cold standalone target",
        "deliver": f"whatsapp:{target_chat}",
        "origin": {
            "platform": "whatsapp",
            "chat_id": "origin@lid",
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
                     "mirror_delivery_scope": "target",
                 }
             },
         ), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(return_value={"success": True}),
         ), \
         patch("gateway.session.SessionStore", return_value=session_store) as store_cls, \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror:
        error = _deliver_result(
            job,
            "Cold destination can continue this briefing.",
            adapters=None,
            loop=None,
        )

    assert error is None
    store_cls.assert_called_once_with(cfg.sessions_dir, cfg)
    session_store.get_or_create_session.assert_called_once()
    seeded = session_store.get_or_create_session.call_args.args[0]
    assert str(seeded.chat_id) == target_chat
    assert str(seeded.user_id) == origin_user
    assert seeded.chat_type == "group"
    mirror.assert_called_once()


def test_target_scope_classifies_explicit_signal_dm_from_live_chat_info():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    class _SignalDMAdapter(MagicMock):
        async def get_chat_info(self, chat_id):
            return {"chat_id": chat_id, "type": "dm"}

    target_chat = "+15551234567"
    pconfig = MagicMock(enabled=True, extra={})
    cfg = MagicMock(platforms={Platform.SIGNAL: pconfig})
    adapter = _SignalDMAdapter()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, raw_response={}))
    adapter.create_handoff_thread = AsyncMock(return_value=None)
    adapter._session_store = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    job = {
        "id": "explicit-signal-dm",
        "name": "Explicit Signal DM",
        "deliver": f"signal:{target_chat}",
        "origin": {
            "platform": "whatsapp",
            "chat_id": "owner@lid",
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
                     "mirror_delivery_scope": "target",
                 }
             },
         ), \
         patch("agent.async_utils.safe_schedule_threadsafe", side_effect=_run_coro_now), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        error = _deliver_result(
            job,
            "Continue this Signal DM.",
            adapters={Platform.SIGNAL: adapter},
            loop=loop,
        )

    assert error is None
    adapter._session_store.get_or_create_session.assert_called_once()
    seeded = adapter._session_store.get_or_create_session.call_args.args[0]
    assert str(seeded.chat_id) == target_chat
    assert seeded.chat_type == "dm"


def test_target_scope_does_not_apply_origin_dm_type_to_distinct_signal_group():
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    class _SignalGroupAdapter(MagicMock):
        async def get_chat_info(self, chat_id):
            return {"chat_id": chat_id, "type": "group"}

    target_chat = "group:target-group"
    pconfig = MagicMock(enabled=True, extra={})
    cfg = MagicMock(platforms={Platform.SIGNAL: pconfig})
    adapter = _SignalGroupAdapter()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, raw_response={}))
    adapter.create_handoff_thread = AsyncMock(return_value=None)
    adapter._session_store = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    job = {
        "id": "distinct-signal-group",
        "name": "Distinct Signal group",
        "deliver": f"signal:{target_chat}",
        "origin": {
            "platform": "signal",
            "chat_id": "+15557654321",
            "chat_type": "dm",
            "user_id": "+15557654321",
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
         patch("gateway.mirror.mirror_to_session", return_value=True):
        error = _deliver_result(
            job,
            "Continue this Signal group.",
            adapters={Platform.SIGNAL: adapter},
            loop=loop,
        )

    assert error is None
    adapter._session_store.get_or_create_session.assert_called_once()
    seeded = adapter._session_store.get_or_create_session.call_args.args[0]
    assert str(seeded.chat_id) == target_chat
    assert seeded.chat_type == "group"
