"""对外快照与编辑器实时状态的小查询。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
import time
from typing import Any
from ..config_scope import session_scoped
from ..chat_browse import thought_visible
from ..decider import reply_willingness
from ..events import (
    ABILITIES,
    ABILITY_LABELS,
    ability_hint,
    normalize_abilities,
    thread_step_lines,
)
from ..models import ECHO_EVENT_TYPES
from ..mood import day_mood_info
from ..prompt import (
    WEEKDAY_NAMES,
    clock_text,
)
from ..state import WorldState
from ..life_matters import ensure as ensure_matters
from ..timeline import render_event
from .constants import (
    DEBUG_ECHO_DEBOUNCE_SECONDS,
    DEBUG_ONLY_ECHO_TYPES,
    EXTENSION_LAYER_HEADING,
    PROMPT_SNAPSHOT_KEEP,
    TICK_LOG_KEEP,
    _DEBUG_ECHO_TYPES,
)
from .models import TickOutcome
from .util import _echo_payload


def _is_tick_summary(note: str) -> bool:
    """``t=1902 node=loft_tatami_corner state=idle mood=…`` 那行机器摘要。

    运行记录里已经有结构化字段了，这行不用再重复显示一遍。
    """

    return note.startswith("t=") and " node=" in note


class SnapshotMixin:
    # ================= 对外快照 =================

    # ---------------- 编辑器「实时状态」用的几个小查询 ----------------

    WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def _clock_text(self) -> str:
        """现实时间 + 时段。用的是提示词同一套写法，免得两边说法不一致。"""

        try:
            return clock_text(self.local_now())
        except Exception:
            return ""

    @staticmethod
    def _duration_text(seconds: Any) -> str:
        """把秒数说成人话（世界时间对照那一行用）。"""

        try:
            total = max(0, int(round(float(seconds))))
        except (TypeError, ValueError):
            return ""
        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分"
        return f"{minutes} 分钟"

    def _next_schedule(self, session_id: str, *, state=None, persona_key: str | None = None) -> dict[str, Any] | None:
        """下一条要触发的日程：时间、还有多少分钟、里面有哪些动作。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return None
        try:
            now = self.local_now()
        except Exception:
            return None
        action_map = self.world.action_map()
        best: dict[str, Any] | None = None
        for item in items:
            if not item.enabled:
                continue
            if not self.schedule_in_scope(item, session_id):
                continue
            if item.matter_id and state is not None:
                from .. import life_matters as matters
                matter = matters.find(state, item.matter_id)
                if (not matter or not matters.visible(matter, session_id)
                        or not matters.belongs_to(matter, persona_key)
                        or matter["status"] in matters.CLOSED | {"paused"}):
                    continue
            chain = self._schedule_life_chain(item)
            if not chain and not item.matter_id:
                continue
            moments = []
            if state is not None:
                delay = self._schedule_delay_record(state, item.id)
                retry_day = self._previous_schedule_delay(state, item, now)
                if not delay.get("cancelled") and (delay.get("day") == now.date().isoformat()
                                                   or retry_day and delay.get("day") == retry_day):
                    try:
                        until = float(delay.get("until") or 0)
                        if until > 0:
                            moments.append(datetime.fromtimestamp(until, now.tzinfo))
                    except (OverflowError, OSError, TypeError, ValueError):
                        pass
            try:
                hour, minute = (int(part) for part in str(item.time).split(":")[:2])
            except (TypeError, ValueError):
                continue
            offsets = range(0, 8)
            if item.once and item.date:
                try:
                    offsets = [(date.fromisoformat(item.date) - now.date()).days]
                except ValueError:
                    continue
            for offset in offsets:
                moment = (now + timedelta(days=offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if moment <= now:
                    continue
                if item.once and item.date and moment.date().isoformat() != item.date:
                    continue
                weekday = self.WEEKDAY_KEYS[moment.weekday()]
                if not item.once and item.days and weekday not in item.days:
                    continue
                moments.append(moment)
                break
            if not moments:
                continue
            moment = min(moments)
            minutes = max(0, int(round((moment - now).total_seconds() / 60)))
            if best is None or minutes < int(best["in_minutes"]):
                names = []
                for step in chain:
                    definition = action_map.get(step.type)
                    names.append((definition.name or definition.id) if definition else step.type)
                best = {
                    "id": item.id,
                    "time": moment.strftime("%H:%M"),
                    "in_minutes": minutes,
                    "weekday": WEEKDAY_NAMES[moment.weekday()],
                    "actions": "接续已有牵挂" if item.matter_id else " → ".join(names),
                    "auto_travel": bool(item.auto_travel),
                }
        return best

    def _budget(self, state: WorldState) -> dict[str, dict[str, int]]:
        """本小时还剩多少额度（跨小时自动归零，和运行时用的是同一套计数）。"""

        hour = self._hour_index(state)
        limits = self.world.limits

        def quota(count: Any, marker: Any, limit: Any) -> dict[str, int]:
            total = max(0, int(limit))
            used = int(count) if int(marker or 0) == hour else 0
            return {"left": max(0, total - used), "used": min(used, total), "limit": total}

        return {
            "plan": quota(
                state.llm_plan_count_hour, state.llm_plan_hour_marker, limits.max_llm_plan_per_hour
            ),
            "text": quota(
                state.llm_text_count_hour, state.llm_text_hour_marker, limits.max_llm_text_per_hour
            ),
            "tool_param": quota(
                state.tool_param_count_hour,
                state.tool_param_hour_marker,
                limits.max_tool_param_per_hour,
            ),
            "share": quota(
                state.share_count_hour, state.share_hour_marker, limits.max_share_per_hour
            ),
            "autonomous": quota(
                state.autonomous_count_hour,
                state.autonomous_hour_marker,
                limits.max_autonomous_per_hour,
            ),
        }

    def _channel_status(self, session_id: str) -> dict[str, Any]:
        """发送通道与最近一次模型调用的状态（排查"她怎么不说话了"用）。"""

        blocked_seconds = 0
        try:
            getter = getattr(self.messenger, "blocked_seconds", None)
            if callable(getter):
                blocked_seconds = int(getter(session_id) or 0)
            elif self.messenger is not None and self.messenger.blocked(session_id):
                blocked_seconds = -1
        except Exception:
            blocked_seconds = 0
        record = dict(self._last_llm.get(session_id) or {})
        if record:
            record["ago_seconds"] = max(0, int(self._now() - float(record.get("at", 0.0))))
        return {
            "send_blocked_seconds": blocked_seconds,
            "last_llm": record,
            # 空串 = 跟随 AstrBot 当前会话的供应商
            "llm_provider": str(getattr(self.llm, "provider_id", "") or ""),
            "has_llm": self.llm is not None,
        }

    @session_scoped
    async def snapshot(self, session_id: str) -> dict[str, Any]:
        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        zone = self.world.zone_map().get(self.world.zone_of(state.node_id))
        # 平台上的真实群名片：别人（dynamic_card_plus 等）改名后，编辑器要能看到当前值。
        # 被别人接管时顺手把本地记录对齐，修掉"名字卡在第一次改过的那一版"的旧数据。
        owner = self.nickname_owner(session_id)
        live_card = (
            await self.live_group_card(session_id)
            if ":GroupMessage:" in session_id
            else ""
        )
        if live_card and owner:
            if await self.align_owned_nickname(session_id, live_card):
                state = await self.load_state(session_id, cold_start=False)
        _, event_persona = await self._journal_persona(session_id)
        event_help = self._pending_event_help(state, event_persona)
        return {
            "session_id": session_id,
            "world_time": state.world_time,
            "tick_seconds": self.tick_seconds,
            "node_id": state.node_id,
            "node_name": node.name if node else "",
            "state": state.state,
            # 扩展想显示的状态（例如"兴奋中"）：只给编辑器看，不进群名片
            "extension_status": self.extensions.status_text(state, session_id),
            "mood": state.mood,
            # 今天的基调（每天掷一次）：只调各条曲线的快慢，界面上说清是哪一种
            "day_mood": {
                "id": str(state.day_mood or ""),
                "label": (
                    day_mood_info(state.day_mood).label
                    if day_mood_info(state.day_mood) is not None
                    else ""
                ),
                "hint": (
                    day_mood_info(state.day_mood).hint
                    if day_mood_info(state.day_mood) is not None
                    else ""
                ),
                "day": str(state.day_mood_day or ""),
                "enabled": bool(getattr(self.world.state_dynamics, "daily_mood_enabled", True)),
            },
            "values": self.dynamics.values(state),
            # 困不困 / 答应了"等会儿再睡"到什么时候（面板与编辑器看这一段）
            "sleep": {
                "mode": self.world.sleep.mode,
                "rhythm": dict(state.sleep_rhythm),
                "history": list(state.sleep_history),
                "pressure": round(
                    max(0.0, min(1.0, float(state.sleep_pressure or 0.0))), 4
                ),
                "threshold": float(
                    getattr(getattr(self.world, "sleep", None), "pressure_threshold", 0.85)
                    or 0.85
                ),
                "delayed": bool(state.sleep_delayed),
                "delay_until": int(state.sleep_delay_until or 0),
                "delay_minutes": self.sleep_delay_minutes(state),
                "delay_reason": str(state.sleep_delay_reason or ""),
                "delay_by": str(state.sleep_delay_by or ""),
                "no_sleep_until": int(state.no_sleep_until or 0),
            },
            # 夜里没有 / 她正睡着的时候，编辑器把「睡眠压力」那一条藏起来：
            # 判定跟主插件自己用的那套（``world.night`` + 本地时钟）保持一致
            "night": {
                "now": bool(self.world.is_night_time(self.local_now().hour)),
                "sleeping": bool(state.is_sleeping),
                "start_hour": int(getattr(getattr(self.world, "night", None), "start_hour", 23) or 0),
                "end_hour": int(getattr(getattr(self.world, "night", None), "end_hour", 7) or 0),
            },
            # 情绪两轴的派生结果：心情词、正在气头上的标记、这一轮的表达格
            "storm": bool(state.storm),
            "style": {
                "cell": state.last_style_cell,
                "say_limit": int(state.last_say_limit or 0),
            },
            "interject": dict(state.interject_stats or {}),
            "participation": dict(state.participation.get(session_id) or {}),
            "tool_breakers": self.tool_breaker_state(),
            "current_action": {key: value for key, value in state.current_action.items()
                               if not key.startswith("browse_") and key != "identity"} if state.current_action else None,
            "activity": self.activity_view(state),
            "life_matters": [item for item in ensure_matters(state, self._now())
                             if not item.get("who") or item.get("session") == session_id],
            "life_recovery": state.life_recovery,
            "life_interests": state.life_interests,
            "life_decision": state.life_decision,
            "activity_reason": str((state.current_plan or {}).get("reason") or ""),
            "current_plan": state.current_plan,
            "last_reasoning": state.reasoning_for(session_id),
            "thoughts": [
                item for item in state.thoughts
                if thought_visible(
                    item, session_id, self._now(), fallback_session=state.session_id
                )
            ][-5:],
            # 别的插件的状态（今日穿搭、背包…）：编辑器里能看到、能手清
            "external_state": dict(getattr(state, "external_state", None) or {}),
            "unanswered_count": state.unanswered_count,
            "cooldown_until": state.cooldown_until,
            # 决策意愿，以及它换算出来的「问大模型」概率（编辑器里显示，方便调参）
            "willingness": round(reply_willingness(state), 4),
            "llm_sample_rate": round(self.decider.llm_sample_rate(state), 4),
            "recent_events": state.recent_events[-10:],
            # 事件系统：能力值（人话）、未了的事、正在等谁拿主意
            "abilities": {
                name: {
                    "label": ABILITY_LABELS[name],
                    "value": round(normalize_abilities(state.abilities or self.world.abilities.initial)[name], 3),
                    "hint": ability_hint(
                        normalize_abilities(state.abilities or self.world.abilities.initial)[name]
                    ),
                }
                for name in ABILITIES
            },
            "event_threads": [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "tier": item.get("tier"),
                    "steps": thread_step_lines(item),
                    "pending_followup": item.get("pending_followup"),
                    "next_step_at": item.get("next_step_at"),
                    "updated_at": item.get("updated_at"),
                }
                for item in self._event_threads(state, event_persona)[-4:][::-1]
            ],
            "pending_help": {
                "state": str(event_help.get("state") or ""),
                "title": str(event_help.get("title") or ""),
                "until": float(
                    event_help.get("active_until")
                    or event_help.get("idle_until")
                    or 0.0
                ),
            },
            "event_digest": self._event_digest_for(state, event_persona),
            "events_enabled": bool(getattr(self.world.events, "enabled", True)),
            # 本会话近期消息与话题备忘，编辑器可查看和清空。
            **self._chat_counts(state, session_id),
            "chat_note": self.chat_note_for(state, session_id),
            "chat_notes": {
                str(key): str(dict(value or {}).get("text") or "")
                for key, value in dict(state.chat_notes or {}).items()
                if isinstance(value, dict)
            },
            "nickname": state.bot_current_nickname or state.bot_base_nickname,
            "nickname_live": live_card,
            "nickname_owner": owner,
            "zone_id": zone.id if zone is not None else "",
            "zone_name": zone.name if zone is not None else "",
            "clock_text": self._clock_text(),
            "world_elapsed_seconds": int(state.world_time * self.tick_seconds),
            "world_elapsed_text": self._duration_text(state.world_time * self.tick_seconds),
            "next_schedule": self._next_schedule(session_id, state=state,
                persona_key=sha256((await self._persona_text(session_id)).encode("utf-8")).hexdigest()[:24]),
            "budget": self._budget(state),
            # 「聊天这条路的情绪额度」用得怎么样了：调情绪量程时看这个最直观
            "chat_mood": {
                "turn_cap": round(
                    float(getattr(self.world.state_dynamics, "chat_valence_cap", 0.05) or 0.0),
                    4,
                ),
                "daily_cap": round(
                    float(
                        getattr(self.world.state_dynamics, "chat_valence_daily_cap", 0.15)
                        or 0.0
                    ),
                    4,
                ),
                "spent": round(float(state.chat_valence_spent or 0.0), 4),
                "day": str(state.chat_day or ""),
            },
            # 「还没聊完的事」：状态页能看到她在惦记什么
            "open_topics": [
                {
                    "text": str(item.get("text") or ""),
                    "who": str(item.get("who") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "asked": int(item.get("asked") or 0),
                    "next_ask_at": float(item.get("next_ask_at") or 0.0),
                }
                for item in (state.open_topics or [])
                if isinstance(item, dict)
            ],
            # 「她记着的账」与「她自己的事」：状态页/调试里也能看见
            "grudges": [
                {
                    "user_id": str(item.get("user_id") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "reason": str(item.get("reason") or ""),
                    "at": float(item.get("at") or 0.0),
                    "until": float(item.get("until") or 0.0),
                }
                for item in (state.grudges or [])
                if isinstance(item, dict)
            ],
            "own_topics": [
                {
                    "text": str(item.get("text") or ""),
                    "who": str(item.get("who") or ""),
                    "who_name": str(item.get("who_name") or ""),
                    "at": float(item.get("at") or 0.0),
                }
                for item in (state.own_topics or [])
                if isinstance(item, dict)
            ],
            "heart_knots": [
                {
                    "text": str(item.get("text") or ""),
                    "about": str(item.get("about") or ""),
                    "since": float(item.get("since") or 0.0),
                }
                for item in (state.heart_knots or [])
                if isinstance(item, dict)
            ],
            "channel": self._channel_status(session_id),
            # 名片是否被锁住（编辑器里用一个按钮切换，所以要能读到当前状态）
            "nickname_locked": bool(state.bot_nickname_locked),
            "nickname_base": state.bot_base_nickname,
            # 按最近说话时间排好、只带前几个：状态页一行放得下，也不会越攒越长
            "user_presence": state.recent_active_users(8),
            "user_presence_total": len(state.user_presence),
            # 「她想找谁」：想念值 / 冷却中的冷却时间 / 今天的主动额度
            "miss": self.miss_overview(state),
            # 「想被碰一碰」：欲求值、触发线、今天还能推几次（和想念并排显示）
            "desire": self.desire_panel(state),
            # 从当前位置能直达哪里、各需要多少 tick（给编辑器和调试看）
            "travel": [
                {
                    "node_id": target,
                    "name": (
                        self.node(target).name if self.node(target) is not None else target
                    ),
                    "ticks": ticks,
                }
                for target, ticks in sorted(
                    self.world.adjacent().get(state.node_id, []),
                    key=lambda item: item[1],
                )
            ],
        }

    @session_scoped
    async def thoughts_payload(self, session_id: str, limit: int = 300) -> dict[str, Any]:
        """「内心活动」那一页要的数据：她自己留下的心里话，越新越靠前。"""

        if not session_id:
            return {"session": "", "thoughts": []}
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return {"session": session_id, "thoughts": []}
        rows: list[dict[str, Any]] = []
        for item in list(state.thoughts or [])[-max(1, int(limit)):]:
            if not isinstance(item, dict) or not thought_visible(
                item, session_id, self._now(), fallback_session=state.session_id
            ):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            node_id = str(item.get("node_id") or "")
            node = self.node(node_id)
            rows.append(
                {
                    "content": content,
                    # 老记录里只有世界时钟（那是个计数器，不是时间戳），
                    # 读不出来时前端按"没有时间"处理，不硬编一个假时间。
                    "at": float(item.get("at") or 0.0),
                    "world_time": float(item.get("world_time") or 0.0),
                    "node_id": node_id,
                    "node_name": (node.name or node.id) if node is not None else "",
                    "source": str(item.get("source") or ""),
                    "target": str(item.get("target") or ""),
                    "quote": str(item.get("quote") or ""),
                    "reply": str(item.get("reply") or ""),
                    "decision_id": str(item.get("decision_id") or ""),
                    "detail": dict(item.get("detail") or {}),
                    "browse_session": str(item.get("browse_session") or ""),
                    "browse_label": self.session_label(item["browse_session"], state) if item.get("browse_session") else "",
                    "browse_sources": list(item.get("browse_sources") or []),
                    "browse_replied": bool(item.get("browse_replied")),
                }
            )
        rows.reverse()
        recent = bool(state.life_updated_at and self._now() - state.life_updated_at < max(180, self.tick_seconds * 3))
        enabled = self.is_enabled(session_id)
        action = state.current_action or {}
        label = self.world.action_map().get(str(action.get("type") or ""))
        current = "正在睡觉" if state.is_sleeping else (label.name if label else "安静活动或休息")
        background = await self.db.call("background_status", day=self._today_key(self._now()))
        call_limit = max(0, int(self.world.limits.background_daily_calls))
        token_limit = max(0, int(self.world.limits.background_daily_tokens))
        calls_exhausted = bool(call_limit and int(background.get("calls") or 0) >= call_limit)
        tokens_exhausted = bool(
            token_limit
            and int(background.get("tokens") or 0) + int(background.get("reserved") or 0) >= token_limit
        )
        autonomous_exhausted = calls_exhausted or tokens_exhausted
        message = (
            "生活时钟正常；自主模型预算已用完，规则生活继续"
            if recent and enabled and autonomous_exhausted
            else "生活时钟正常"
            if recent and enabled
            else "此会话尚未启用生活"
            if not enabled
            else "暂未检测到近期生活时钟，请检查运行页"
        )
        note = (
            "这里只记录实际留下的想法。聊后复盘与手动生成不占自主生活预算；"
            "预算用完后，规则决定仍会留下简短选择依据。"
        )
        return {"session": session_id, "thoughts": rows, "browse": self.chat_browse_status(state, session_id), "health": {
            "enabled": enabled, "clock_recent": recent,
            "last_tick_at": state.life_updated_at, "last_plan_at": state.last_llm_plan_at,
            "last_thought_at": max((float(row.get("at") or 0) for row in rows), default=0.0),
            "thought_count": len(rows),
            "autonomous_generation": "budget_exhausted" if autonomous_exhausted else "available",
            "background_budget": {
                **background,
                "call_limit": call_limit,
                "token_limit": token_limit,
                "remaining_calls": max(0, call_limit - int(background.get("calls") or 0)) if call_limit else None,
                "remaining_tokens": (
                    max(0, token_limit - int(background.get("tokens") or 0) - int(background.get("reserved") or 0))
                    if token_limit else None
                ),
            },
            "current": current,
            "message": message,
            "note": note,
        }}

    async def clear_thoughts(self, session_id: str, category: str = "all") -> int:
        """清掉留下的心里话（只删这一份记录，记忆和日志都不动）。"""

        if not session_id:
            return 0
        async with self.session_state(session_id) as state:
            before = len(state.thoughts)
            state.thoughts = [
                item for item in state.thoughts
                if (category == "browse" and not item.get("browse_session"))
                or (category == "inner" and item.get("browse_session"))
                or not thought_visible(
                    item, session_id, 0, fallback_session=state.session_id
                )
            ]
            removed = before - len(state.thoughts)
        return removed

    async def preview_last_reply_request(self, session_id: str) -> str:
        """只读展示已记录阶段的请求，不冒充供应商最终请求。"""

        import json

        snapshot = next(
            (
                item
                for item in self._prompt_snapshots
                if str(item.get("session_id") or "") == str(session_id)
            ),
            None,
        )
        if snapshot is None:
            return (
                f"会话 {session_id} 还没有接管回复的记录：\n"
                "先让她回一次话，这里就会显示上一次请求快照及其记录阶段\n"
                "（system prompt / 宿主历史 / 用户消息 / 临时附加内容与本轮 batch）。"
            )
        contexts = list(snapshot.get("contexts") or [])
        extra = list(snapshot.get("extra_user_content_parts") or [])
        stage = {
            "native_hook_complete": "My World 注入完成（on_llm_request 钩子结束前）",
            "engine_prepared": "My World 引擎准备完成（尚未合并宿主请求）",
            "legacy_before_model": "旧版接管调用模型前",
        }.get(snapshot.get("stage"), "引擎准备阶段")
        parts = [
            "# ===== 最近一次请求快照 =====\n"
            f"会话：{snapshot.get('session_id') or ''}\n"
            f"时间戳：{snapshot.get('at') or 0}\n"
            f"记录阶段：{stage}\n"
            "后续钩子、工具循环及供应商适配仍可能修改请求；此记录不证明已调用或发送成功。\n"
            "只保存在内存中，不写入宿主历史；媒体载荷省略，超限截断时以下内容不完整。\n"
            f"记录前大小：{json.dumps(snapshot.get('request_sizes', {}), ensure_ascii=False)}\n"
            "文本统计按系统提示、用户消息、宿主历史和临时附加内容分别计字符，不是 Token；"
            "不含媒体载荷与工具参数。text_scan_complete=false 时仅为已扫描部分，不改写任何插件内容。\n"
            f"媒体与工具：{json.dumps(snapshot.get('media', {}), ensure_ascii=False)}\n"
            f"工具名：{json.dumps(snapshot.get('tool_names', []), ensure_ascii=False)}\n"
        ]
        if snapshot.get("snapshot_limits", {}).get("truncated"):
            parts.append("注意：这份快照已截断；历史优先保留最近 128 条，其余集合最多 128 项。")
        keys = [str(item) for item in (snapshot.get("batch_keys") or [])]
        texts = [str(item) for item in (snapshot.get("batch_texts") or [])]
        parts.append("本轮 batch 的事件身份（按到达顺序）：")
        for index, key in enumerate(keys):
            text = texts[index] if index < len(texts) else ""
            parts.append(f"· {key or '（无身份）'}：{text}")
        parts.append(f"\n# ===== 1. system prompt =====\n{snapshot.get('system_prompt') or ''}")
        parts.append(
            "# ===== 2. 宿主历史（AstrBot contexts）=====\n"
            + (json.dumps(contexts, ensure_ascii=False, indent=2) if contexts else "（没有记录到宿主历史）")
        )
        parts.append(f"# ===== 3. 最后一条用户消息 =====\n{snapshot.get('user_prompt') or ''}")
        parts.append("# ===== 4. 临时附加内容（extra_user_content_parts）=====\n"
                     + json.dumps(extra, ensure_ascii=False, indent=2))
        if snapshot.get("runtime_part"):
            parts.append("引擎待注入的动态上下文：\n" + snapshot["runtime_part"])
        if snapshot.get("original_user_text"):
            parts.append("本轮事件原始用户文本（诊断对照，不是额外发送一遍）：\n"
                         + snapshot["original_user_text"])
        if snapshot.get("context_selection"):
            parts.append("# ===== 5. 本轮上下文取材依据 =====\n" + json.dumps(
                snapshot["context_selection"], ensure_ascii=False, indent=2))
        return "\n".join(parts)

    def _remember_prompt_snapshot(
        self,
        *,
        session_id: str,
        system_prompt: str,
        user_prompt: str,
        contexts: Any,
        batch: list[dict[str, Any]],
        chat_rows: list[dict[str, Any]],
        context_selection: dict | None = None,
        runtime_part: str = "",
        stage: str = "engine_prepared",
        image_urls: Any = (),
    ) -> dict[str, Any] | None:
        """留一份有界副本；注入完成后由调用方携带身份补全，避免会话并发串轮。"""

        try:
            from ..prompt_snapshot import MAX_ITEMS, bounded_snapshot, request_sizes

            entries = self.reply_batch_entries(batch, chat_rows=chat_rows)
            snapshot = bounded_snapshot({
                    "session_id": str(session_id),
                    "at": self._now(),
                    "stage": stage,
                    "request_sizes": request_sizes(system_prompt, user_prompt, contexts),
                    "media": {"image_urls": len(image_urls or [])},
                    "batch_keys": [str(item.get("event_key") or "") for item in entries],
                    "batch_texts": [str(item.get("text") or "") for item in entries],
                    "context_selection": context_selection or {},
                    "system_prompt": str(system_prompt or ""),
                    "user_prompt": str(user_prompt or ""),
                    "runtime_part": runtime_part,
                    "contexts": (contexts or [])[-MAX_ITEMS:],
                })
            snapshot["snapshot_limits"]["truncated"] |= len(contexts or []) > MAX_ITEMS
            self._prompt_snapshots.insert(0, snapshot)
            del self._prompt_snapshots[PROMPT_SNAPSHOT_KEEP:]
            return snapshot
        except Exception as exc:  # 调试快照绝不能影响回复本身
            self._log("debug", f"请求快照没留住：{exc}")
            return None

    def complete_prompt_snapshot(self, snapshot, req, *, original_user_text: str = "") -> None:
        """记录本钩子完成后的宿主请求；不改请求、历史、工具或其他插件的临时标记。"""

        if snapshot is None:
            return
        try:
            from ..prompt_snapshot import MAX_ITEMS, bounded_snapshot, request_sizes

            system = getattr(req, "system_prompt", "") or ""
            prompt = getattr(req, "prompt", "") or ""
            contexts = getattr(req, "contexts", None) or []
            extra = getattr(req, "extra_user_content_parts", None) or []
            tools = getattr(getattr(req, "func_tool", None), "tools", None) or []
            completed = bounded_snapshot({
                "session_id": snapshot["session_id"], "at": self._now(),
                "stage": "native_hook_complete",
                "request_sizes": request_sizes(system, prompt, contexts, extra),
                "media": {"image_urls": len(getattr(req, "image_urls", None) or []),
                          "audio_urls": len(getattr(req, "audio_urls", None) or []),
                          "tools": len(tools)},
                "batch_keys": snapshot.get("batch_keys", []),
                "batch_texts": snapshot.get("batch_texts", []),
                "context_selection": snapshot.get("context_selection", {}),
                "system_prompt": system, "user_prompt": prompt,
                "original_user_text": original_user_text,
                "extra_user_content_parts": extra,
                "contexts": contexts[-MAX_ITEMS:],
                "tool_names": [getattr(tool, "name", "") for tool in tools[:MAX_ITEMS]],
            })
            completed["snapshot_limits"]["truncated"] |= (
                len(contexts) > MAX_ITEMS or len(tools) > MAX_ITEMS
                or bool(snapshot.get("snapshot_limits", {}).get("truncated")))
            snapshot.clear()
            snapshot.update(completed)
        except Exception as exc:
            self._log("debug", f"宿主请求快照没补全：{exc}")

    async def overview(self) -> list[dict[str, Any]]:
        """所有启用会话的简要状态：编辑器地图页一次看全"谁在哪"。"""

        result: list[dict[str, Any]] = []
        for session_id in self.enabled_session_ids():
            try:
                with self.config_scope(session_id):
                    state = await self.load_state(session_id, cold_start=False)
                    node = self.node(state.node_id)
                    activity = self.activity_view(state)
            except Exception:
                continue
            result.append(
                {
                    "session_id": session_id,
                    "state_key": self.state_key(session_id),
                    "node_id": state.node_id,
                    "node_name": node.name if node is not None else state.node_id,
                    "state": state.state,
                    "mood": state.mood,
                    "world_time": state.world_time,
                    "activity": activity,
                }
            )
        return result

    async def preview_autonomous_prompt(self, session_id: str, persona_id: str = "") -> str:
        """预览接管模式会发给 LLM 的 system prompt（只读）。"""

        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        _cell, say_limit, style_text = self.style_for(state, session_id, remember=False)
        request = self.context_request(state, session_id, task="plan", persona=persona_id)
        persona = await self._persona_text(session_id)
        request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
        text = self.prompts.build_autonomous_system_prompt(
            persona_text=persona,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.context_memories(state, request),
            context_request=request,
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            # 预览和实跑必须同源：窗口也用 session_id 这个目标会话取
            recent_chat=self.context_chat(state, request),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            # 预览要和实跑一致：自主提示词里也要有"你能说话的地方"
            session_directory=self.session_directory(state),
            current_session=session_id,
            session_labels=self.session_labels(state),
            # 配额用完的动作，实跑时不会出现在提示词里，预览也不该出现
            hidden_actions=self.exhausted_actions(state),
            profile_text=self.context_profiles(state, request),
            # 声音样例也要一致：预览里看不到，用户就没法确认挑中的那几句到底进没进
            samples=self.voice_sample_lines(state, session_id, preview=True),
            # 预览要跟实跑一致：有扩展声明要"他碰了她哪儿"时，这里也得带上那个字段
            wants_touch=self.extensions.wants("touch"),
            json_fields=self.extensions.json_fields(),
            # 预览要跟实跑一致：实跑会带上「好奇心」那一段，这里也要带
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=True, include_life=False)] if note
            ],
            **await self.runtime_notes(session_id),
        )
        # 预览要和实跑一致：实跑会带上扩展的注入层（engine 里的自主计划那条路），
        # 以前这里漏了，调试页看"自主提示词"就像没装扩展一样
        layer = self._extension_prompt(state, session_id)
        return f"{text}\n\n{EXTENSION_LAYER_HEADING}\n{layer}" if layer else text

    async def preview_voice_samples(self, session_id: str) -> list[dict[str, Any]]:
        """调试页用：**这一轮会抽到哪几条声音样例**。

        提示词两万字，样例排在第 2 段，不点出来基本找不着——顺手连"为什么是这几条"
        （各自的场景标签）一起给。
        """

        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return []
        picked = self.voice_sample_lines(state, session_id, preview=True)
        labels = {
            key: str(label).split("：")[0] for key, label in self.prompts.VOICE_SCENES
        }
        return [
            {
                "text": str(item.get("text") or ""),
                "scene": str(item.get("scene") or ""),
                "scene_label": labels.get(str(item.get("scene") or ""), "") or "不限",
                "move": str(item.get("move") or ""),
            }
            for item in picked
        ]

    async def preview_state_slots(self, session_id: str) -> list[dict[str, Any]]:
        """调试页用：她当前的状态槽，以及每一条会不会写进提示词。

        过期的那几条**不会**进提示词，调试页要能说清这件事，
        不然只会看到"状态槽里明明有，提示词里却没有"。
        """

        state = await self.load_state(session_id, cold_start=False)
        now = self._now()
        slots: list[dict[str, Any]] = []
        for slot, info in dict(getattr(state, "external_state", None) or {}).items():
            if not isinstance(info, dict):
                continue
            try:
                expires = float(info.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            slots.append(
                {
                    "slot": str(slot),
                    "label": str(info.get("label") or slot),
                    "text": str(info.get("text") or ""),
                    "at": float(info.get("at") or 0.0),
                    "expired": bool(expires and now > expires),
                }
            )
        return slots

    async def _event_marker(self, state: WorldState) -> int:
        """本轮的起点事件 id。

        第一次用到（插件刚启动 / 刚重载）时要从库里取当前最大 id——不能默认 0，
        否则「把新事件发到群里」会把历史上最后 50 条日志当成新事件重放一遍，
        变成每个 tick 往群里刷一串老消息。
        """

        known = self._event_ids.get(state.session_id)
        if known is not None:
            return int(known)
        latest = 0
        try:
            rows = await self.db.call(
                "query_events", session_id=state.session_id, limit=1
            )
            if rows:
                latest = int(rows[0].get("id") or 0)
        except Exception:
            latest = 0
        self._event_ids[state.session_id] = latest
        return latest

    def echo_types(self) -> set[str]:
        """「调试输出」勾选的事件类型（只认认识的 key，空集合 = 关闭）。"""

        return set(self.echo_modes())

    def echo_modes(self) -> dict[str, str]:
        """每个类型各自的显示方式：``full`` / ``compact``（``off`` 的不列出来）。"""

        # 扩展注册的类型也算数：「调试输出」勾了它，就照它的方式发到群里
        known = set(ECHO_EVENT_TYPES) | self.extensions.debug_event_types()
        modes: dict[str, str] = {}
        for name, mode in dict(getattr(self.world, "echo_modes", {}) or {}).items():
            key = str(name).strip()
            value = str(mode or "").strip().lower()
            if key in known and value in ("full", "compact"):
                modes[key] = value
        if modes:
            return modes
        # 兼容：还没迁移过的老配置
        legacy = {
            str(name).strip()
            for name in (self.world.echo_types or [])
            if str(name).strip() in known
        }
        fallback = "compact" if self.echo_compact() else "full"
        return {name: fallback for name in legacy}

    def _ext_echo_icon(self, event_type: str) -> str:
        """扩展注册的类型在调试输出里用什么图标。"""

        key = str(event_type or "")
        for item in self.extensions.debug_events():
            if str(item.get("type") or "") == key:
                return str(item.get("icon") or "•")
        return "•"

    def echo_compact_for(self, event_type: str) -> bool:
        """这一类调试输出是不是精简模式。"""

        return str((self.echo_modes() or {}).get(str(event_type or ""), "full")) == "compact"

    def echo_compact(self) -> bool:
        """精简模式：调试输出只发事件本身，不带参数与结果。"""

        return bool(getattr(self.world, "echo_compact", False))

    def _queue_echo(self, session_id: str, lines: list[str]) -> None:
        """回复路径之外的调试回显先存着，等下一次发送时一起带出去。

        （被叫醒、睡觉门禁这类事件发生在"还没决定要不要回复"之前，
        没法挂在某一轮回复的 outcome 上。）
        """

        if not lines:
            return
        self._pending_echo.setdefault(session_id, []).extend(
            line for line in lines if line
        )

    def take_pending_echo(self, session_id: str) -> list[str]:
        """取走并清空攒下的回显（发送方负责带到群里）。"""

        return self._pending_echo.pop(session_id, [])

    async def _send_debug_live(self, session_id: str, message: str) -> bool:
        """把一条调试行立刻发到群里（发送失败冷却、分段逻辑都在 messenger 里）。"""

        text = str(message or "").strip()
        if self.debug_sink is None or not text:
            return False
        try:
            return bool(await self.debug_sink(session_id, text))
        except Exception as exc:
            self._log("debug", f"调试回显发送失败：{exc}")
            return False

    def _echo_debounced(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        """同一工具的连续调用要不要吞掉：一串并行查询只留第一条。"""

        if event_type not in _DEBUG_ECHO_TYPES:
            return False
        tool = str(payload.get("tool") or payload.get("command") or "")
        if not tool:
            return False
        key = (session_id, event_type, tool)
        now = time.monotonic()
        last = self._echo_debounce.get(key, 0.0)
        self._echo_debounce[key] = now
        if len(self._echo_debounce) > 200:
            self._echo_debounce.clear()
            self._echo_debounce[key] = now
        return bool(last and (now - last) < DEBUG_ECHO_DEBOUNCE_SECONDS)

    async def _echo_events_since(
        self, state: WorldState, outcome: TickOutcome | None, marker: int
    ) -> None:
        """把这一轮新产生的事件渲染成消息（只处理「调试输出」里勾选了的类型）。"""

        if outcome is None:
            return
        enabled = self.echo_types()
        if not enabled:
            return
        try:
            events = await self.db.call(
                "query_events", session_id=state.session_id, limit=50
            )
        except Exception:
            return
        fresh = [item for item in events if int(item.get("id") or 0) > marker]
        # 处理过就把游标推到最后一条：不然这一批会在下一个 tick 再发一遍
        # （tick 没有新事件时游标不会自己前进，这就是"同一句话一直刷"的来源）。
        self._event_ids[state.session_id] = max(
            [marker] + [int(item.get("id") or 0) for item in events]
        )
        # query_events 是新到旧；一次 tick 里最多回显最新的这么多条，
        # 再按时间正序发出去（连锁动作多的时候不要把群刷了）
        modes = self.echo_modes()
        if not modes:
            return
        # 有些事件在发生的当下就已经插好位置了（见 ``_log_event`` 的 ``outcome`` 参数），
        # 这里再发一遍就会重复——跳过它们，剩下的才补在这一轮末尾。
        already = set(getattr(outcome, "echoed_event_ids", ()) or ())
        already |= set(self._echoed_ids.get(state.session_id, ()) or ())
        for item in reversed(fresh[:12]):
            if int(item.get("id") or 0) in already:
                continue
            kind = str(item.get("event_type") or "")
            detail = item.get("detail")
            if not isinstance(detail, dict):
                detail = {}
            if not _echo_payload(kind, detail, enabled):
                continue
            mode = modes.get(kind)
            if not mode:
                continue
            if kind in DEBUG_ONLY_ECHO_TYPES and mode == "compact":
                continue
            try:
                line = render_event(
                    item,
                    self.world,
                    compact=mode == "compact",
                    ext_render=self.extensions.debug_event_text,
                )
            except Exception:
                continue
            if line:
                icon = ECHO_EVENT_TYPES.get(kind) or self._ext_echo_icon(kind)
                outcome.add_debug(f"{icon} {line}".strip())

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        if level == "debug" and not self.debug:
            return
        method = getattr(self.logger, level, None)
        if callable(method):
            method(f"[my_world] {message}")

    # ---------------- 运行记录：tick 只进这里，不进后台日志 ----------------

    def _record_tick(self, outcomes: list[TickOutcome]) -> None:
        """把这一轮 tick 收进内存环，供编辑器的「运行记录」页查看。

        tick 属于运行时噪音：以前它会以 INFO 写进 AstrBot 后台日志，翻起来全是
        「t=1902 node=…」。现在只在内存里留最近 ``TICK_LOG_KEEP`` 拍。
        """

        self._tick_log.appendleft(
            {
                "at": self._now(),
                "count": len(outcomes),
                "sessions": [
                    {
                        "session_id": str(outcome.session_id or ""),
                        "summary": dict(outcome.summary or {}),
                        "notes": [
                            str(note)
                            for note in (outcome.notes or [])
                            if str(note).strip() and not _is_tick_summary(str(note))
                        ],
                        "messages": [
                            str(item)
                            for item in (outcome.messages or [])
                            if str(item).strip()
                        ],
                    }
                    for outcome in outcomes
                ],
            }
        )

    def tick_log_payload(self) -> dict[str, Any]:
        """「运行记录」页要的数据：最近几拍她分别在哪儿、做了什么、说了什么。"""

        return {
            "keep": TICK_LOG_KEEP,
            "at": self._now(),
            "entries": [dict(entry) for entry in self._tick_log],
        }

    async def _log_event(
        self,
        state: WorldState,
        event_type: str,
        detail: dict[str, Any] | None = None,
        *,
        persona_id: str = "",
        outcome: TickOutcome | None = None,
        silent: bool = False,
        place: str = "",
    ) -> None:
        """写一条事件日志（编辑器的「日志」页会渲染成人话），并顺手做数量裁剪。

        传入 ``outcome`` 且开启了「把动作发到群里」时，同一行文本也会作为消息发出去。
        ``silent=True`` 表示"这条已经并进别的行里了"：不发，但要让收尾那次批量回显跳过它。

        ``place`` / ``outcome.place`` 是"这件事发生在哪个会话"：和她的存档会话不是同一个时，
        事件里会多记一条 ``session``（渲染出来就是「私聊 123456「主人」」这种），
        这样一份日志里也能分清哪句是群里说的、哪句是私聊说的。
        """

        payload = detail or {}
        if not persona_id:
            try:
                _, persona_id = await self._journal_persona(state.session_id)
            except Exception:
                persona_id = ""
        where = str(place or (getattr(outcome, "place", "") if outcome else "") or "")
        if where and where != str(state.session_id):
            label = self.session_label(where, state)
            if label:
                payload = {**payload, "session": label}
        enabled = self.echo_types()
        line_added = False
        line = ""
        muted = event_type in DEBUG_ONLY_ECHO_TYPES and self.echo_compact_for(event_type)
        if (
            not silent
            and not muted
            and enabled
            and _echo_payload(event_type, payload, enabled)
        ):
            try:
                line = render_event(
                    {
                        "event_type": event_type,
                        "detail": payload,
                        "world_time": state.world_time,
                    },
                    self.world,
                    compact=self.echo_compact_for(event_type),
                    ext_render=self.extensions.debug_event_text,
                )
            except Exception:
                line = ""
        consumed = False
        live = False
        # 回显发到"这件事发生的那个会话"：她在私聊里被搭话、调了工具，
        # 那几行回显就该出现在私聊，而不是她存档所在的那个群
        echo_target = where or str(state.session_id)
        if line and not self.speech_allowed(echo_target, outcome.speech_kind if outcome is not None else ""):
            line = ""
            consumed = True  # Keep diagnostics in the event log, never queue a blocked echo.
        if line:
            rendered = f"{ECHO_EVENT_TYPES.get(event_type, '•')} {line}"
            if self._echo_debounced(echo_target, event_type, payload):
                # 同一工具的连续调用（一次检索并行查好几条）只发第一条
                consumed = True
            elif self.debug_sink is not None:
                live = await self._send_debug_live(echo_target, rendered)
            elif outcome is not None:
                outcome.add_debug(rendered)
                line_added = True

        try:
            new_id = await self.db.call(
                "add_event",
                session_id=state.session_id,
                persona_id=persona_id,
                world_time=state.world_time,
                event_type=event_type,
                detail=payload,
            )
            if new_id:
                self._event_ids[state.session_id] = int(new_id)
                if outcome is not None and (line_added or silent or live or consumed):
                    # 这一行已经在发生的当下插好了位置：收尾那次批量回显要跳过它
                    outcome.echoed_event_ids.add(int(new_id))
                if live or consumed:
                    self._echoed_ids.setdefault(state.session_id, set()).add(int(new_id))
        except Exception as exc:  # 日志写失败不能影响主流程
            self._log("debug", f"写事件日志失败：{exc}")
            return
        count = self._event_writes.get(state.session_id, 0) + 1
        self._event_writes[state.session_id] = count
        if count % 100 == 0:
            try:
                await self.db.call(
                    "prune_session_events",
                    state.session_id,
                    int(self.world.limits.max_log_events),
                )
            except Exception:
                pass
