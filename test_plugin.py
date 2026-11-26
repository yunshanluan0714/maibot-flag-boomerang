"""SDK integration regressions for scope and participant identity."""

import asyncio
import importlib
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(importlib.util.find_spec("maibot_sdk"), "requires the real MaiBot SDK")
class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        module = importlib.import_module(f"{Path(__file__).resolve().parent.name}.plugin")
        self.plugin = module.FlagBoomerangPlugin()
        self.plugin.set_plugin_config(self.plugin.build_default_config())
        self.trigger = AsyncMock()
        self.plugin._set_context(SimpleNamespace(
            logger=logging.getLogger(__name__),
            api=SimpleNamespace(call=AsyncMock(return_value=[{"user_id": "31", "card": "甲"}])),
            maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=self.trigger)),
        ))
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.plugin._store = module.FlagStore(data.name)
        self.plugin._running = True

    async def test_manual_identity_and_scope(self):
        context = {"stream_id": "qq:group:21", "group_id": "21", "user_id": "32"}
        result = await self.plugin.tool_manage_flag(action="record", user_name="甲", content="明天完成画稿", **context)
        self.assertTrue(result["success"])
        self.assertEqual(self.plugin._store.list_flags("21")[0].user_id, "31")
        self.plugin.ctx.api.call.return_value.append({"user_id": "33", "card": "甲"})
        result = await self.plugin.tool_manage_flag(action="record", user_name="甲", content="明天完成画稿", **context)
        self.assertFalse(result["success"])
        self.plugin.config.capture.blocked_groups = ["21"]
        result = await self.plugin.tool_query_flags(**context)
        self.assertFalse(result["success"])
        result = await self.plugin.tool_manage_flag(action="record", user_name="甲", content="明天完成画稿", **context)
        self.assertFalse(result["success"])
        self.plugin._pending_hints["qq:group:21"] = "hint"
        planner = await self.plugin.on_planner_before_request(session_id="qq:group:21", items=[])
        self.assertEqual(planner["modified_kwargs"]["items"], [])

    async def test_deadline_respects_opt_out_and_charges_only_accepted_triggers(self):
        now = time.time()
        self.plugin.config.trigger.quiet_start = self.plugin.config.trigger.quiet_end = "00:00"
        self.plugin._last_group_activity["21"] = now
        kwargs = dict(group_id="21", session_id="qq:group:21", user_id="31", user_name="甲",
                      category="deadline_goal", created_at=now-1000, deadline_ts=now-100)
        first, _ = self.plugin._store.add_flag(original_text="旧约定", summary="旧约定", **kwargs)
        self.plugin.config.capture.blocked_groups = ["21"]
        await self.plugin.check_due_flags_once(now_ts=now)
        self.trigger.assert_not_awaited()
        self.assertEqual(self.plugin._store.find_flag_by_id("21", first.id).status, "active")

        self.plugin.config.capture.blocked_groups = []
        self.trigger.return_value = {"success": False}
        await self.plugin.check_due_flags_once(now_ts=now)
        self.assertEqual(self.plugin._store.find_flag_by_id("21", first.id).status, "due_wait")
        self.assertFalse(self.plugin._proactive_daily_count)

        second, _ = self.plugin._store.add_flag(original_text="新约定", summary="新约定", **kwargs)
        self.trigger.return_value = {"success": True}
        await self.plugin.check_due_flags_once(now_ts=now)
        self.assertEqual(self.plugin._store.find_flag_by_id("21", second.id).status, "reminded")
        self.assertEqual(self.plugin._proactive_daily_count["21"][1], 1)

    async def test_legacy_manual_record_waits_for_unique_identity(self):
        record, _ = self.plugin._store.add_flag(group_id="21", session_id="qq:group:21", user_id="manual:甲",
                                               user_name="甲", original_text="旧约定", summary="旧约定",
                                               category="deadline_goal", created_at=1)
        pending = self.plugin._store.pending_manual_names()
        self.assertEqual(pending, [("21", "甲")])
        self.plugin.ctx.api.call.return_value.append({"user_id": "33", "card": "甲"})
        await self.plugin._bind_legacy_participants(pending)
        self.assertEqual(self.plugin._store.pending_manual_names(), pending)
        self.plugin.ctx.api.call.return_value.pop()
        await self.plugin._bind_legacy_participants(pending)
        self.assertEqual(self.plugin._store.pending_manual_names(), [])
        self.assertEqual(self.plugin._store.find_flag_by_id("21", record.id).user_id, "31")

    async def test_disable_scope_after_receive_before_background_processing(self):
        self.plugin.config.capture.use_llm_verification = False
        self.plugin.config.trigger.quiet_start = self.plugin.config.trigger.quiet_end = "00:00"
        now = time.time()
        existing, _ = self.plugin._store.add_flag(
            group_id="21", session_id="qq:group:21", user_id="31", user_name="甲", category="sleep_vow",
            original_text="早点睡", summary="早点睡", created_at=now-3600, sleep_cutoff_ts=now-300,
            sleep_window_end_ts=now+600,
        )
        await self.plugin.on_receive_after_process(message={
            "message_id": "msg", "session_id": "qq:group:21", "processed_plain_text": "今晚我一定十二点前睡觉",
            "message_info": {"group_info": {"group_id": "21"}, "user_info": {"user_id": "31"}},
        })
        self.assertEqual(len(self.plugin._bg_tasks), 1)
        self.plugin.config.capture.blocked_groups = ["21"]
        await self.plugin.on_config_update("self", {}, "test")
        await asyncio.gather(*self.plugin._bg_tasks)
        self.assertEqual(self.plugin._store.find_flag_by_id("21", existing.id).status, "active")
        self.assertFalse(self.plugin._pending_hints)

    async def test_dynamic_adapter_discovery_and_local_member_fallback(self):
        # 1) Dynamic discovery via ctx.api.list() works for third-party OneBot/NapCat adapters.
        async def custom_call(api_name, **kwargs):
            if api_name == "third-party.onebot.action.call":
                return {"success": True, "result": {"data": [{"user_id": "88", "nickname": "乙"}]}}
            return {"success": False, "error": "not found"}

        self.plugin._member_api_targets = None
        self.plugin.ctx.api = SimpleNamespace(
            list=AsyncMock(return_value={"apis": [{"plugin_id": "third-party.onebot", "name": "action.call"}]}),
            call=AsyncMock(side_effect=custom_call),
        )
        res = await self.plugin.tool_manage_flag(
            action="record", user_name="乙", content="明天背单词", stream_id="qq:group:21", group_id="21", user_id="32"
        )
        self.assertTrue(res["success"])
        self.assertEqual(self.plugin._store.list_flags("21")[0].user_id, "88")

        # 2) When adapter API is unavailable, falls back to locally observed members.
        self.plugin._member_api_targets = None
        self.plugin.ctx.api = SimpleNamespace(call=AsyncMock(return_value={"success": False, "error": "offline"}))
        self.plugin._known_members["21"] = {"99": "丙"}
        res2 = await self.plugin.tool_manage_flag(
            action="record", user_name="丙", content="本周写完作业", stream_id="qq:group:21", group_id="21", user_id="32"
        )
        self.assertTrue(res2["success"])
        self.assertEqual(self.plugin._store.list_flags("21", user_name="丙")[0].user_id, "99")
