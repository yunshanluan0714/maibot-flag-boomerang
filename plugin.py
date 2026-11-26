"""Main plugin wiring for github.yunshanluan0714.flag-boomerang v1.0.0."""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Any
from uuid import uuid4

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import FlagBoomerangConfig
from .detector import (
    build_bust_verify_prompt,
    build_extract_prompt,
    evaluate_outrageous_bust,
    fallback_parse_candidate,
    in_quiet_hours,
    match_keyword_bust,
    parse_chinese_deadline,
    parse_llm_bust_detail,
    parse_llm_extract_response,
    prefilter_flag_candidate,
    resolve_tz,
    sanitize_incoming_text,
)
from .store import FlagRecord, FlagStore

_STATUS_LABEL = {
    "active": "待检验",
    "busted": "已打脸",
    "pending_roast": "待追责",
    "due_wait": "已到期待追问",
    "reminded": "已验收到期",
    "fulfilled": "已兑现销账",
    "expired": "已归档",
}
_CATEGORY_LABEL = {
    "sleep_vow": "早睡誓言",
    "spend_gacha_vow": "戒氪/戒断",
    "deadline_goal": "限时Flag",
    "bold_prediction": "毒奶/狠话",
}


def _extract_llm_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for key in ("response", "text", "content"):
            val = resp.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


def _build_reminder_item(text: str) -> dict[str, Any]:
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat()},
        "parts": [{"type": "text", "text": text}],
    }


def _inject_reminder(items: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    updated = list(items)
    pos = len(updated)
    if updated and updated[-1].get("item_type") == "UserMessageItem":
        parts = [str(p.get("text") or "") for p in updated[-1].get("parts") or [] if isinstance(p, dict)]
        if parts and not parts[0].lstrip().startswith("<message"):
            pos -= 1
    updated.insert(pos, _build_reminder_item(text))
    return updated


class FlagBoomerangPlugin(MaiBotPlugin):
    """群聊 Flag 验尸官 / 回旋镖追踪器插件。"""

    config_model = FlagBoomerangConfig

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._store: FlagStore | None = None
        self._bot_qq_account = ""
        self._bot_nickname = ""
        self._session_to_group: dict[str, str] = {}
        self._session_to_private: dict[str, str] = {}
        self._known_members: dict[str, dict[str, str]] = {}
        self._member_api_targets: list[tuple[str, str]] | None = None
        self._pending_hints: dict[str, str] = {}
        self._last_group_activity: dict[str, float] = {}
        self._proactive_inflight: set[str] = set()
        self._last_proactive_at: dict[str, float] = {}
        self._proactive_daily_count: dict[str, tuple[str, int]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._deadline_task: asyncio.Task[None] | None = None

    async def on_load(self) -> None:
        self._store = FlagStore(self.ctx.paths.data_dir)
        await self._refresh_bot_identity()
        await self._warm_stream_mappings()
        self._running = True
        pending = self._store.pending_manual_names()
        if pending:
            task = asyncio.create_task(self._bind_legacy_participants(pending))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        self._deadline_task = asyncio.create_task(self._run_deadline_loop(), name="flag-boomerang-deadline")
        self.ctx.logger.info("群聊 Flag 回旋镖追踪器 v1.1.1 已加载")

    async def on_unload(self) -> None:
        self._running = False
        if self._deadline_task is not None:
            self._deadline_task.cancel()
            try:
                await self._deadline_task
            except asyncio.CancelledError:
                pass
            self._deadline_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        for session_id in list(self._pending_hints):
            if not self._scope_allowed(self._resolve_scope_group_id(session_id)):
                self._pending_hints.pop(session_id, None)
        self.ctx.logger.info("群聊 Flag 回旋镖配置已热更新: scope=%s", scope)

    async def _refresh_bot_identity(self) -> None:
        try:
            qq = await self.ctx.config.get("bot.qq_account", "")
            nick = await self.ctx.config.get("bot.nickname", "")
            if isinstance(qq, str) and qq.strip():
                self._bot_qq_account = qq.strip()
            if isinstance(nick, str) and nick.strip():
                self._bot_nickname = nick.strip()
        except Exception:
            pass

    async def _warm_stream_mappings(self) -> None:
        try:
            getter = getattr(self.ctx.chat, "get_all_streams", None) or getattr(self.ctx.chat, "get_group_streams", None)
            if getter is None:
                return
            raw = await getter(platform="qq")
            streams = raw.get("streams", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            if not isinstance(streams, list):
                return
            for st in streams:
                if not isinstance(st, dict):
                    continue
                sid = str(st.get("session_id") or st.get("stream_id") or "").strip()
                if not sid:
                    continue
                gid = str(st.get("group_id") or "").strip()
                uid = str(st.get("user_id") or "").strip()
                if st.get("is_group_session") or st.get("chat_type") == "group" or gid:
                    if gid:
                        self._session_to_group[sid] = gid
                else:
                    self._session_to_group.setdefault(sid, "")
                    if uid:
                        self._session_to_private[sid] = uid
        except Exception:
            pass

    def _resolve_scope_group_id(self, session_id: str, *, explicit_group_id: str = "", user_id: str = "") -> str:
        if explicit_group_id:
            return explicit_group_id
        sid = str(session_id or "").strip()
        if sid in self._session_to_group:
            gid = self._session_to_group[sid]
            if gid:
                return gid
        if ":group:" in sid:
            return sid.split(":group:")[-1]
        puid = user_id or self._session_to_private.get(sid, "")
        if not puid and ":user:" in sid:
            puid = sid.split(":user:")[-1]
        return f"private:{puid}" if puid else ""

    def _is_scope_enabled(self, *, group_id: str, is_private: bool) -> bool:
        cfg = self.config
        if not cfg.plugin.enabled:
            return False
        if is_private:
            return bool(cfg.capture.allow_private)
        blocked = {str(x).strip() for x in cfg.capture.blocked_groups if str(x).strip()}
        if group_id in blocked:
            return False
        allowed = {str(x).strip() for x in cfg.capture.allowed_groups if str(x).strip()}
        if allowed and group_id not in allowed:
            return False
        return True

    def _scope_allowed(self, group_id: str) -> bool:
        return bool(group_id) and self._is_scope_enabled(group_id=group_id, is_private=group_id.startswith("private:"))

    async def _discover_member_api_targets(self) -> list[tuple[str, str]]:
        if self._member_api_targets is not None:
            return self._member_api_targets
        defaults: list[tuple[str, str]] = [
            ("call_data", "maibot-team.snowluma-adapter.adapter.snowluma.action.call_data"),
            ("call_data", "maibot-team.snowluma-adapter.adapter.napcat.action.call_data"),
            ("call_data", "maibot-team.snowluma-adapter.adapter.onebot.action.call_data"),
        ]
        list_fn = getattr(getattr(self.ctx, "api", None), "list", None)
        if not callable(list_fn):
            self._member_api_targets = defaults
            return self._member_api_targets
        try:
            payload = await list_fn()
        except Exception:
            self._member_api_targets = defaults
            return self._member_api_targets
        items = payload.get("apis", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        discovered: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for kind, suffix in (
            ("call_data", "action.call_data"),
            ("call", "action.call"),
            ("direct", "get_group_member_list"),
        ):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                pid = str(entry.get("plugin_id") or "").strip()
                name = str(entry.get("name") or "").strip()
                if not pid or not name:
                    continue
                if name == suffix or name.endswith("." + suffix):
                    full = name if name.startswith(pid + ".") else f"{pid}.{name}"
                    pair = (kind, full)
                    if pair not in seen:
                        seen.add(pair)
                        discovered.append(pair)
        self._member_api_targets = discovered or defaults
        return self._member_api_targets

    @staticmethod
    def _unwrap_member_list(resp: Any) -> list[dict[str, Any]]:
        if isinstance(resp, dict):
            if resp.get("success") is False:
                return []
            if "result" in resp:
                resp = resp["result"]
            if isinstance(resp, dict) and "data" in resp:
                resp = resp["data"]
        return [m for m in resp if isinstance(m, dict)] if isinstance(resp, list) else []

    def _local_known_members(self, group_id: str) -> list[dict[str, str]]:
        by_uid: dict[str, dict[str, str]] = {}
        if self._store is not None:
            for item in self._store.list_known_members(group_id):
                uid = str(item.get("user_id") or "").strip()
                if uid:
                    by_uid[uid] = item
        for uid, uname in self._known_members.get(group_id, {}).items():
            if uid:
                by_uid[uid] = {"user_id": uid, "card": uname, "nickname": uname}
        return list(by_uid.values())

    async def _group_members(self, group_id: str) -> list:
        api_cap = getattr(self.ctx, "api", None)
        call_fn = getattr(api_cap, "call", None)
        if callable(call_fn):
            targets = await self._discover_member_api_targets()
            for kind, api_name in targets:
                try:
                    if kind == "direct":
                        resp = await call_fn(api_name, group_id=group_id)
                    else:
                        resp = await call_fn(
                            api_name,
                            action_name="get_group_member_list",
                            params={"group_id": group_id},
                        )
                except Exception:
                    continue
                members = self._unwrap_member_list(resp)
                if members:
                    return members
        return self._local_known_members(group_id)

    async def _manual_participant(
        self, group_id: str, target: str, sender_id: str, members: list | None = None
    ) -> tuple[str, str] | None:
        target = target.strip()
        if group_id.startswith("private:"):
            uid = group_id.removeprefix("private:")
            if target.isdigit() and target != uid:
                return None
            return uid, target or uid
        if sender_id and (not target or target == sender_id):
            return sender_id, sender_id
        if not target:
            return None
        if members is None:
            members = await self._group_members(group_id)
        matches = {}
        for member in members:
            if not isinstance(member, dict):
                continue
            uid = str(member.get("user_id") or "").strip()
            card = str(member.get("card") or "").strip()
            nickname = str(member.get("nickname") or "").strip()
            if uid and target == uid:
                return uid, card or nickname or uid
            if uid and target in (card, nickname):
                matches[uid] = card or nickname or uid
        return next(iter(matches.items())) if len(matches) == 1 else None

    async def _bind_legacy_participants(self, pending: list[tuple[str, str]]) -> None:
        members_by_group = {}
        for group_id, name in pending:
            if not self._scope_allowed(group_id):
                continue
            if not group_id.startswith("private:") and group_id not in members_by_group:
                members_by_group[group_id] = await self._group_members(group_id)
            person = await self._manual_participant(group_id, name, "", members_by_group.get(group_id))
            if person and self._store and self._scope_allowed(group_id):
                self._store.bind_manual_name(group_id, name, person[0])

    def _now_dt(self, now_ts: float | None = None) -> datetime:
        tz = resolve_tz(self.config.trigger.timezone)
        if now_ts is None:
            return datetime.now(tz)
        return datetime.fromtimestamp(float(now_ts), tz=tz)

    def _can_proactive_in_group(self, group_id: str, now_dt: datetime) -> bool:
        cfg = self.config.trigger
        if not self._scope_allowed(group_id) or group_id in self._proactive_inflight or cfg.proactive_daily_limit_per_group <= 0:
            return False
        if in_quiet_hours(now_dt, cfg.quiet_start, cfg.quiet_end):
            return False
        now_ts = now_dt.timestamp()
        last_at = self._last_proactive_at.get(group_id, 0.0)
        if now_ts - last_at < float(cfg.proactive_cooldown_minutes) * 60.0:
            return False
        day_key = now_dt.strftime("%Y-%m-%d")
        prev_day, cnt = self._proactive_daily_count.get(group_id, (day_key, 0))
        if prev_day != day_key:
            cnt = 0
        return cnt < cfg.proactive_daily_limit_per_group

    def _mark_proactive_sent(self, group_id: str, now_dt: datetime) -> None:
        day_key = now_dt.strftime("%Y-%m-%d")
        prev_day, cnt = self._proactive_daily_count.get(group_id, (day_key, 0))
        if prev_day != day_key:
            cnt = 0
        self._last_proactive_at[group_id] = now_dt.timestamp()
        self._proactive_daily_count[group_id] = (day_key, cnt + 1)

    @staticmethod
    def _format_roast_hint(flag: FlagRecord, *, kind: str, new_text: str = "", reason: str = "") -> str:
        dt_created = datetime.fromtimestamp(flag.created_at).strftime("%m-%d %H:%M")
        extra = f"（原因：{reason}）" if reason else ""
        if kind == "sleep_bust":
            return (
                f"[回旋镖抓现行] {flag.user_name} 在 {dt_created} 刚立过早睡Flag：「{flag.original_text}」，"
                f"刚才又冒泡发了：「{new_text[:60]}」。顺嘴精准递回旋镖打趣一两句即可。"
            )
        if kind == "keyword_bust":
            return (
                f"[回旋镖抓现行] {flag.user_name} 在 {dt_created} 曾放话：「{flag.original_text}」，"
                f"刚才发言：「{new_text[:60]}」{extra}。像损友一样顺手调侃两句。"
            )
        return (
            f"[回旋镖抓现行] {flag.user_name} 之前立过的Flag「{flag.original_text}」（{dt_created}）"
            "已经到了验收节点，趁他刚冒泡顺嘴问一句兑现了没有。"
        )

    @HookHandler(
        "chat.receive.after_process",
        name="flag_boomerang_observe_incoming",
        description="静默监听群友立 Flag 与破戒打脸信号（非阻塞）",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=4000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_receive_after_process(self, **kwargs: Any) -> None:
        if not self._running or not self.config.plugin.enabled or self._store is None:
            return
        msg = kwargs.get("message")
        if not isinstance(msg, dict):
            return

        msg_info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
        user_info = msg_info.get("user_info") if isinstance(msg_info.get("user_info"), dict) else {}
        group_info = msg_info.get("group_info") if isinstance(msg_info.get("group_info"), dict) else None
        user_id = str(user_info.get("user_id") or "").strip()
        if not user_id or (self._bot_qq_account and user_id == self._bot_qq_account):
            return

        sender_name = str(user_info.get("user_cardname") or user_info.get("user_nickname") or user_id).strip()
        raw_group_id = str(group_info.get("group_id") or "").strip() if group_info else ""
        is_private = not bool(raw_group_id)
        session_id = str(msg.get("session_id") or "").strip()
        platform = str(msg_info.get("platform") or "qq").strip()
        if not session_id and platform:
            session_id = f"{platform}:{'group:' + raw_group_id if raw_group_id else 'user:' + user_id}"
        scope_group_id = raw_group_id or f"private:{user_id}"

        if session_id:
            self._session_to_group[session_id] = raw_group_id
            if is_private:
                self._session_to_private[session_id] = user_id

        if not self._is_scope_enabled(group_id=scope_group_id, is_private=is_private):
            return

        plain_text = str(msg.get("processed_plain_text") or "").strip()
        if not plain_text:
            return
        now_ts = time.time()
        self._last_group_activity[scope_group_id] = now_ts
        if not is_private and sender_name:
            self._known_members.setdefault(scope_group_id, {})[user_id] = sender_name

        task = asyncio.create_task(
            self._process_incoming_message(
                scope_group_id=scope_group_id,
                session_id=session_id,
                user_id=user_id,
                sender_name=sender_name,
                message_id=str(msg.get("message_id") or "").strip(),
                plain_text=plain_text,
                now_ts=now_ts,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _process_incoming_message(
        self,
        *,
        scope_group_id: str,
        session_id: str,
        user_id: str,
        sender_name: str,
        message_id: str,
        plain_text: str,
        now_ts: float,
    ) -> None:
        if self._store is None or not self._scope_allowed(scope_group_id):
            return
        cfg = self.config
        now_dt = self._now_dt(now_ts)
        quiet = in_quiet_hours(now_dt, cfg.trigger.quiet_start, cfg.trigger.quiet_end)
        clean_text = sanitize_incoming_text(plain_text)

        try:
            # 1. 白天交付昨晚暂存的 pending_roast / due_wait
            if not quiet:
                pending = self._store.get_pending_for_user(scope_group_id, user_id)
                if pending:
                    top = pending[0]
                    if top.status == "pending_roast":
                        self._store.mark_busted(
                            top.id,
                            busted_at=top.busted_at or now_ts,
                            bust_text=top.bust_text,
                            bust_message_id=top.bust_message_id,
                        )
                        self._pending_hints[session_id] = self._format_roast_hint(
                            top,
                            kind="sleep_bust",
                            new_text=top.bust_text or clean_text,
                        )
                        return
                    if top.status == "due_wait":
                        self._store.mark_reminded(top.id)
                        self._pending_hints[session_id] = self._format_roast_hint(top, kind="due")
                        return

            active_flags = self._store.get_active_for_user(scope_group_id, user_id)
            busted_now = False

            # 2. 早睡誓言实时抓现行
            if cfg.trigger.enable_sleep_bust and active_flags:
                for flag in active_flags:
                    if (
                        flag.category == "sleep_vow"
                        and flag.sleep_cutoff_ts is not None
                        and flag.sleep_window_end_ts is not None
                        and flag.sleep_cutoff_ts <= now_ts <= flag.sleep_window_end_ts
                    ):
                        if quiet:
                            self._store.mark_pending_roast(
                                flag.id,
                                busted_at=now_ts,
                                bust_text=clean_text,
                                bust_message_id=message_id,
                            )
                            self.ctx.logger.info("凌晨静默期暂存熬夜破戒: id=%d user=%s", flag.id, sender_name)
                            await self._maybe_save_outrageous_memory(
                                flag,
                                bust_text=clean_text,
                                busted_at=now_ts,
                                reason="凌晨仍熬夜破戒",
                            )
                        else:
                            self._store.mark_busted(
                                flag.id,
                                busted_at=now_ts,
                                bust_text=clean_text,
                                bust_message_id=message_id,
                            )
                            await self._maybe_save_outrageous_memory(
                                flag,
                                bust_text=clean_text,
                                busted_at=now_ts,
                                reason="超出早睡截止时间仍在发言",
                            )
                            if not self._scope_allowed(scope_group_id):
                                return
                            self._pending_hints[session_id] = self._format_roast_hint(
                                flag,
                                kind="sleep_bust",
                                new_text=clean_text,
                            )
                            await self._trigger_proactive(
                                scope_group_id=scope_group_id,
                                session_id=session_id,
                                hint=self._pending_hints[session_id],
                                now_dt=now_dt,
                                reason="flag_boomerang_bust",
                            )
                        busted_now = True
                        break

            # 3. 关键词 + LLM 二次核验打脸
            if not busted_now and cfg.trigger.enable_keyword_bust and active_flags:
                for flag in active_flags:
                    if now_ts - flag.created_at < 300.0:
                        continue
                    hits = match_keyword_bust(clean_text, flag)
                    if not hits:
                        continue
                    confirmed = True
                    reason = f"命中「{hits[0]}」"
                    llm_outrageous: bool | None = None
                    if cfg.capture.use_llm_verification:
                        ok_llm, llm_reason, llm_outrageous = await self._verify_bust_with_llm(flag, clean_text)
                        if not ok_llm:
                            confirmed = False
                        elif llm_reason:
                            reason = llm_reason
                    if not self._scope_allowed(scope_group_id):
                        return
                    if confirmed:
                        self._store.mark_busted(
                            flag.id,
                            busted_at=now_ts,
                            bust_text=clean_text,
                            bust_message_id=message_id,
                        )
                        await self._maybe_save_outrageous_memory(
                            flag,
                            bust_text=clean_text,
                            busted_at=now_ts,
                            reason=reason,
                            llm_outrageous=llm_outrageous,
                        )
                        if not self._scope_allowed(scope_group_id):
                            return
                        if not quiet:
                            self._pending_hints[session_id] = self._format_roast_hint(
                                flag,
                                kind="keyword_bust",
                                new_text=clean_text,
                                reason=reason,
                            )
                        busted_now = True
                        break

            # 4. 未触发破功时，评估本条消息本身是否是新立的 Flag
            if not busted_now and cfg.capture.enable_auto_capture:
                cand = prefilter_flag_candidate(plain_text, now_dt)
                if cand is None:
                    return
                parsed = None
                if cfg.capture.use_llm_verification:
                    parsed = await self._extract_flag_with_llm(sender_name, clean_text, cand, now_dt)
                if not self._scope_allowed(scope_group_id):
                    return
                if parsed is None:
                    parsed = fallback_parse_candidate(cand, now_dt)
                if parsed.is_flag and parsed.confidence >= float(cfg.capture.min_confidence):
                    rec, inserted = self._store.add_flag(
                        group_id=scope_group_id,
                        session_id=session_id,
                        user_id=user_id,
                        user_name=sender_name,
                        message_id=message_id,
                        original_text=clean_text,
                        summary=parsed.summary,
                        category=parsed.category,
                        created_at=now_ts,
                        deadline_ts=parsed.deadline_ts,
                        sleep_cutoff_ts=parsed.sleep_cutoff_ts,
                        sleep_window_end_ts=parsed.sleep_window_end_ts,
                        bust_keywords=parsed.bust_keywords,
                        confidence=parsed.confidence,
                        max_active_per_user=cfg.capture.max_active_per_user,
                    )
                    if inserted:
                        self.ctx.logger.info(
                            "静默记下新 Flag: id=%d cat=%s conf=%.2f summary=%s",
                            rec.id,
                            rec.category,
                            rec.confidence,
                            rec.summary,
                        )
        except Exception as exc:
            self.ctx.logger.debug("处理 Flag 候选异常: %s", type(exc).__name__)

    async def _extract_flag_with_llm(self, user_name: str, text: str, cand: Any, now_dt: datetime) -> Any:
        try:
            prompt = build_extract_prompt(user_name, text, now_dt)
            resp = await self.ctx.llm.generate(
                prompt,
                task_name=self.config.capture.llm_task_name,
                temperature=0.1,
                max_tokens=220,
            )
            raw_text = _extract_llm_text(resp)
            if not raw_text:
                return None
            return parse_llm_extract_response(raw_text, cand, now_dt)
        except Exception as exc:
            self.ctx.logger.debug("LLM 抽取 Flag 回落纯规则: %s", type(exc).__name__)
            return None

    async def _verify_bust_with_llm(self, flag: FlagRecord, new_text: str) -> tuple[bool, str, bool | None]:
        try:
            prompt = build_bust_verify_prompt(flag, new_text)
            resp = await self.ctx.llm.generate(
                prompt,
                task_name=self.config.capture.llm_task_name,
                temperature=0.1,
                max_tokens=160,
            )
            raw_text = _extract_llm_text(resp)
            if not raw_text:
                return False, "", None
            return parse_llm_bust_detail(raw_text)
        except Exception:
            return False, "", None

    async def _maybe_save_outrageous_memory(
        self,
        flag: FlagRecord,
        *,
        bust_text: str,
        busted_at: float,
        reason: str = "",
        llm_outrageous: bool | None = None,
    ) -> bool:
        if self._store is None or flag.memory_saved or not self._scope_allowed(flag.group_id):
            return False
        cfg = self.config.trigger
        if not cfg.save_outrageous_to_memory:
            return False

        ev = evaluate_outrageous_bust(
            flag,
            bust_text,
            busted_at,
            llm_outrageous=llm_outrageous,
            min_score=cfg.outrageous_min_score,
        )
        if not ev.is_outrageous:
            return False

        tz = resolve_tz(cfg.timezone)
        dt_created = datetime.fromtimestamp(flag.created_at, tz=tz).strftime("%Y-%m-%d %H:%M")
        dt_busted = datetime.fromtimestamp(busted_at, tz=tz).strftime("%Y-%m-%d %H:%M")
        detail_note = reason or "、".join(ev.reasons)
        memory_text = (
            f"{dt_created}，{flag.user_name}立下Flag：「{flag.original_text}」，"
            f"随后在{dt_busted}因「{bust_text[:80]}」当场打脸（{detail_note}）。"
        )
        is_private = flag.group_id.startswith("private:")
        try:
            res = await self.ctx.call_capability(
                "memory.ingest_text",
                external_id=f"flag_boomerang:{flag.group_id}:{flag.id}",
                source_type="flag_boomerang",
                text=memory_text,
                chat_id=flag.session_id,
                participants=[flag.user_name],
                timestamp=float(busted_at),
                tags=["flag_boomerang", "打脸名场面", flag.category],
                metadata={
                    "flag_id": flag.id,
                    "category": flag.category,
                    "drama_score": ev.score,
                    "user_id": flag.user_id,
                },
                respect_filter=True,
                group_id="" if is_private else flag.group_id,
                user_id=flag.user_id,
            )
            if isinstance(res, dict) and res.get("success"):
                self._store.mark_memory_saved(flag.id)
                self.ctx.logger.info("已将离谱打脸名场面写入长期记忆: id=%d score=%d", flag.id, ev.score)
                return True
        except Exception as exc:
            self.ctx.logger.debug("写入离谱打脸长期记忆跳过: id=%d err=%s", flag.id, type(exc).__name__)
        return False

    async def _trigger_proactive(
        self, *, scope_group_id: str, session_id: str, hint: str, now_dt: datetime, reason: str
    ) -> bool:
        if not session_id or not self._can_proactive_in_group(scope_group_id, now_dt):
            return False
        self._proactive_inflight.add(scope_group_id)
        try:
            result = await self.ctx.maisaka.proactive.trigger(stream_id=session_id, intent=hint, reason=reason)
            accepted = result is True or (isinstance(result, dict) and result.get("success") is True)
            if accepted:
                self._mark_proactive_sent(scope_group_id, now_dt)
            return accepted
        except Exception:
            return False
        finally:
            self._proactive_inflight.discard(scope_group_id)

    @HookHandler(
        "maisaka.planner.before_request",
        name="flag_boomerang_planner_hint",
        description="仅在命中抓现行或到期节点时注入一次性回旋镖线索（常态零注入）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        unchanged = {"action": "continue", "modified_kwargs": kwargs}
        if not self._running or not self.config.plugin.enabled:
            return unchanged
        items = kwargs.get("items")
        if not isinstance(items, list):
            return unchanged
        session_id = str(kwargs.get("session_id") or "").strip()
        hint = self._pending_hints.pop(session_id, "")
        if not hint or not self._scope_allowed(self._resolve_scope_group_id(session_id)):
            return unchanged
        updated = dict(kwargs)
        updated["items"] = _inject_reminder(items, hint)
        return {"action": "continue", "modified_kwargs": updated}

    async def _run_deadline_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(300)
                await self.check_due_flags_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.debug("到期 Flag 巡检异常: %s", type(exc).__name__)

    async def check_due_flags_once(self, *, now_ts: float | None = None) -> None:
        if not self._running or not self.config.plugin.enabled or not self.config.trigger.enable_deadline_check:
            return
        if self._store is None:
            return
        now = float(now_ts if now_ts is not None else time.time())
        now_dt = self._now_dt(now)
        if in_quiet_hours(now_dt, self.config.trigger.quiet_start, self.config.trigger.quiet_end):
            return

        due_items = self._store.find_due_flags(now)
        for flag in due_items:
            if not self._scope_allowed(flag.group_id):
                continue
            last_act = self._last_group_activity.get(flag.group_id, 0.0)
            active_recently = last_act > 0.0 and (now - last_act <= 24 * 3600)
            if active_recently and await self._trigger_proactive(
                scope_group_id=flag.group_id, session_id=flag.session_id,
                hint=self._format_roast_hint(flag, kind="due"), now_dt=now_dt, reason="flag_boomerang_due",
            ):
                self._store.mark_reminded(flag.id)
            else:
                self._store.mark_due_wait(flag.id)

    @Tool(
        "flag_boomerang_query",
        description="查询当前聊天群组内记录的群友 Flag、早睡誓言或历史打脸记录（仅限当前群，绝不跨群泄露）。",
        parameters=[
            ToolParameterInfo(
                name="status_filter",
                param_type=ToolParamType.STRING,
                description="筛选状态：active(进行中，默认)、busted(已打脸)、fulfilled(已兑现)、all(全部)",
                required=False,
                enum_values=["active", "busted", "fulfilled", "all"],
                default="active",
            ),
            ToolParameterInfo(
                name="user_name",
                param_type=ToolParamType.STRING,
                description="可选：按群友昵称或 QQ 号筛选",
                required=False,
            ),
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="可选：按内容关键词筛选",
                required=False,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回条数上限（默认 8，最大 15）",
                required=False,
                default=8,
            ),
        ],
    )
    async def tool_query_flags(
        self,
        status_filter: str = "active",
        user_name: str = "",
        keyword: str = "",
        limit: int = 8,
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._store is None:
            return {"success": False, "content": "Flag 仓库尚未初始化。"}
        group_id = self._resolve_scope_group_id(
            stream_id, explicit_group_id=str(kwargs.get("group_id") or ""), user_id=str(kwargs.get("user_id") or "")
        )
        if not stream_id or not self._scope_allowed(group_id):
            return {"success": False, "content": "无法确认当前所属聊天，出于群隔离限制拒绝查询。"}

        rows = self._store.list_flags(
            group_id,
            status_filter=status_filter,
            user_name=str(user_name or ""),
            keyword=str(keyword or ""),
            limit=limit,
        )
        if not rows:
            return {"success": True, "content": "本群目前还没有符合条件的 Flag 记录。"}

        lines = [f"本群 Flag 档案（共 {len(rows)} 条）："]
        for r in rows:
            created = datetime.fromtimestamp(r.created_at).strftime("%m-%d %H:%M")
            st = _STATUS_LABEL.get(r.status, r.status)
            cat = _CATEGORY_LABEL.get(r.category, r.category)
            line = f"#{r.id} [{st}·{cat}] {r.user_name} ({created}): 「{r.original_text[:60]}」"
            if r.deadline_ts:
                dl = datetime.fromtimestamp(r.deadline_ts).strftime("%m-%d %H:%M")
                line += f" | 期限 {dl}"
            if r.bust_text:
                line += f" | 破戒现场：「{r.bust_text[:40]}」"
            if r.memory_saved:
                line += " [已入长期记忆]"
            lines.append(line)
        return {"success": True, "content": "\n".join(lines)}

    @Tool(
        "flag_boomerang_manage",
        description="在本群内手动登记一条 Flag、帮某人把某条 Flag 标记为已兑现（销账）、或删除一条误记的 Flag。",
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="操作类型：record(补记新Flag)、fulfill(标记已兑现销账)、delete(删除/作废)",
                required=True,
                enum_values=["record", "fulfill", "delete"],
            ),
            ToolParameterInfo(
                name="flag_id",
                param_type=ToolParamType.INTEGER,
                description="fulfill 或 delete 时必填的 Flag 编号",
                required=False,
            ),
            ToolParameterInfo(
                name="user_name",
                param_type=ToolParamType.STRING,
                description="record 时的群友 QQ 号或唯一昵称；私聊可留空",
                required=False,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="record 时的 Flag 原话或承诺内容",
                required=False,
            ),
            ToolParameterInfo(
                name="category",
                param_type=ToolParamType.STRING,
                description="record 时的分类：sleep_vow / spend_gacha_vow / deadline_goal / bold_prediction",
                required=False,
                default="deadline_goal",
            ),
            ToolParameterInfo(
                name="deadline_text",
                param_type=ToolParamType.STRING,
                description="record 时可选的中文期限描述（如 明天、本月底、3天后）",
                required=False,
            ),
        ],
    )
    async def tool_manage_flag(
        self,
        action: str = "",
        flag_id: int | None = None,
        user_name: str = "",
        content: str = "",
        category: str = "deadline_goal",
        deadline_text: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._store is None:
            return {"success": False, "content": "Flag 仓库尚未初始化。"}
        group_id = self._resolve_scope_group_id(
            stream_id, explicit_group_id=str(kwargs.get("group_id") or ""), user_id=str(kwargs.get("user_id") or "")
        )
        if not stream_id or not self._scope_allowed(group_id):
            return {"success": False, "content": "无法确认当前所属聊天，出于群隔离限制拒绝修改。"}

        act = str(action or "").strip().lower()
        if act in ("fulfill", "delete"):
            if flag_id is None:
                return {"success": False, "content": "请提供要操作的 Flag 编号（flag_id）。"}
            target_status = "fulfilled" if act == "fulfill" else "expired"
            updated = self._store.update_status(group_id, int(flag_id), target_status)
            if updated is None:
                return {"success": False, "content": f"本群未找到编号为 #{flag_id} 的 Flag。"}
            verb = "已兑现销账" if act == "fulfill" else "已作废删除"
            return {"success": True, "content": f"Flag #{updated.id}（{updated.user_name}：「{updated.summary}」）{verb}。"}

        if act == "record":
            clean_content = sanitize_incoming_text(content)
            person = await self._manual_participant(group_id, str(user_name or ""), str(kwargs.get("user_id") or ""))
            if not person or not self._scope_allowed(group_id):
                return {"success": False, "content": "无法唯一确认当前会话的成员，请提供当事人的 QQ 号。"}
            participant_id, who = person
            if not clean_content:
                return {"success": False, "content": "请提供要记录的 Flag 具体内容。"}
            cat = category if category in ("sleep_vow", "spend_gacha_vow", "deadline_goal", "bold_prediction") else "deadline_goal"
            now_dt = self._now_dt()
            dl_ts = parse_chinese_deadline(f"{deadline_text} {clean_content}", now_dt)
            cand = prefilter_flag_candidate(clean_content, now_dt)
            parsed = fallback_parse_candidate(cand, now_dt) if cand is not None else None
            rec, _ = self._store.add_flag(
                group_id=group_id,
                session_id=stream_id,
                user_id=participant_id,
                user_name=who,
                original_text=clean_content,
                summary=clean_content[:50],
                category=cat,
                created_at=now_dt.timestamp(),
                deadline_ts=dl_ts,
                sleep_cutoff_ts=parsed.sleep_cutoff_ts if parsed and cat == "sleep_vow" else None,
                sleep_window_end_ts=parsed.sleep_window_end_ts if parsed and cat == "sleep_vow" else None,
                bust_keywords=parsed.bust_keywords if parsed and parsed.category == cat else (),
                confidence=0.95,
                max_active_per_user=self.config.capture.max_active_per_user,
            )
            return {"success": True, "content": f"已为 {who} 登记本群 Flag #{rec.id}：「{rec.summary}」。"}

        return {"success": False, "content": f"不支持的操作：{action}"}


def create_plugin() -> FlagBoomerangPlugin:
    return FlagBoomerangPlugin()
