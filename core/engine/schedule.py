"""时钟与日程：闸门、排期与下一次时间。"""

from __future__ import annotations

from datetime import datetime
import time
from typing import Any
from ..config_scope import session_scoped
from ..json_actions import extract_json_object
from ..models import (
    ActionDef,
    ChainStep,
)
from ..pathfinding import nearest_node
from ..prompt import period_of
from ..state import WorldState
from .models import TickOutcome
from .util import _clip_text


class ScheduleMixin:
    # ================= 时钟 / 日程 =================

    def _now(self) -> float:
        if self.clock is not None:
            return float(self.clock.now())
        return time.time()

    def _resolve_tz(self):
        name = (self.world.timezone or "").strip()
        if not name:
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            return None

    def local_now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now_struct"):
            struct = self.clock.now_struct()
            if isinstance(struct, datetime):
                return struct
        tz = self._resolve_tz()
        if tz is None:
            return datetime.now()
        try:
            return datetime.now(tz)
        except Exception:
            return datetime.now()

    # 日程检查游标最多往前回看多久（现实秒）。用于重启 / 刚启用时不要把很久以前
    # 的日程翻出来补跑。
    SCHEDULE_LOOKBACK_SECONDS = 90
    SCHEDULE_MAX_GAP_SECONDS = 600

    max_followup_chars = 800
    """动作结果交给大模型续说前的截断长度（base64 图片地址不能整条塞进提示词）。"""

    def _clip_followup(self, text: Any) -> str:
        """续说前把结果截断：超长内容（例如 base64 图片）不能整条进提示词。"""

        body = str(text or "").strip()
        if len(body) <= self.max_followup_chars:
            return body
        return body[: self.max_followup_chars] + "…（内容过长已截断）"

    def _schedule_moment(self, now: datetime, schedule: Any) -> datetime | None:
        """这条日程「今天的触发时刻」；时间写得不对就返回 None。"""

        try:
            hour, minute = (int(part) for part in str(schedule.time).split(":")[:2])
        except (TypeError, ValueError):
            return None
        try:
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None

    def _schedule_cursor(self, state: WorldState, now: datetime) -> float:
        """这个会话上次检查到哪一刻（现实时间戳）。"""

        seconds = now.timestamp()
        cursor = float(getattr(state, "schedule_cursor", 0.0) or 0.0)
        if cursor <= 0 or cursor > seconds:
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        if seconds - cursor > self.SCHEDULE_MAX_GAP_SECONDS:
            # 停了很久（重启、插件没跑）就别把这段时间里的日程全补一遍
            return seconds - self.SCHEDULE_LOOKBACK_SECONDS
        return cursor

    def _schedule_reason(self, state: WorldState, conditions: Any) -> str:
        """日程到点却没跑的原因（写进日志，省得用户猜）。"""

        if conditions is None:
            return ""
        if conditions.not_state and state.state in conditions.not_state:
            return f"她现在是「{state.state}」状态"
        if conditions.state and state.state not in conditions.state:
            return f"她现在是「{state.state}」状态，不在允许的列表里"
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return f"精力 {state.energy:.2f} 低于要求的 {conditions.min_energy}"
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return f"精力 {state.energy:.2f} 高于允许的 {conditions.max_energy}"
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return f"孤独感 {state.loneliness:.2f} 低于要求的 {conditions.min_loneliness}"
        if conditions.node_in and state.node_id not in conditions.node_in:
            return f"她不在指定地点（现在在 {state.node_id}）"
        return "条件不满足"

    def node_label(self, node_id: Any) -> str:
        """地点的中文名（拿不到就原样返回 id）。"""

        key = str(node_id or "")
        node = self.node(key)
        return (node.name or node.id) if node is not None else key

    def _chain_labels(self, chain: list[Any] | None) -> list[str]:
        """动作链里每一步的中文名（日程日志用）。"""

        action_map = self.world.action_map()
        labels: list[str] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = action_map.get(action_id)
            labels.append(
                (definition.name or definition.id) if definition else action_id
            )
        return [label for label in labels if label]

    def fallback_intent(self, definition: ActionDef, step: Any = None) -> str:
        """日程里的工具 / 指令步骤没写意图时的兜底说明。

        动作链本来就没有"想干什么"这一栏，缺了它工具型动作会直接跳过
        （"没有给出想做什么"）。这里用动作自己的说明拼一句，让它至少能跑起来；
        用户可以在日程里写清意图把它顶掉。
        """

        label = definition.name or definition.id
        parts = [f"做「{label}」这件事"]
        description = str(definition.description or "").strip()
        if description:
            parts.append(description)
        node_id = str(getattr(step, "target_node", "") or "")
        if node_id:
            parts.append(f"地点是{self.node_label(node_id)}")
        return "：".join([parts[0], "；".join(parts[1:])]) if len(parts) > 1 else parts[0]

    async def fill_schedule_intents(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """保存日程时，给缺意图的工具 / 指令步骤补一句「想干什么」。

      用内容生成模型（`creator_provider_id`，没配就复用主模型）写，写进日程 JSON 里，
        编辑器里能看到也能改。这样运行时不用再花钱、也不会因为缺意图被跳过。
        返回 ``(处理后的配置, 说明列表)``；模型不可用时原样返回。
        """

        if self.creator_llm is None or not isinstance(payload, dict):
            return payload, []
        action_map = self.world.action_map()
        filled: list[str] = []
        for schedule in payload.get("schedules") or []:
            if not isinstance(schedule, dict) or schedule.get("smart"):
                # 智能日程到点自己排计划，不需要每步的意图
                continue
            chain = schedule.get("action_chain") or []
            for index, step in enumerate(chain, start=1):
                if not isinstance(step, dict):
                    continue
                definition = action_map.get(str(step.get("type") or ""))
                if definition is None or definition.llm_level not in ("tool", "command"):
                    continue
                if str(step.get("intent") or "").strip():
                    continue
                intent = await self._generate_intent(definition, schedule)
                if not intent:
                    continue
                step["intent"] = intent
                label = definition.name or definition.id
                filled.append(f"{schedule.get('id') or '日程'} 第 {index} 步「{label}」：{intent}")
        return payload, filled

    async def _generate_intent(self, definition: ActionDef, schedule: Any) -> str:
        """让内容生成模型给一步工具 / 指令动作写一句意图。"""

        label = definition.name or definition.id
        schedule_id = str(schedule.get("id") or "") if isinstance(schedule, dict) else ""
        detail = [f"日程 id：{schedule_id or '（未命名）'}"]
        detail.append(f"这一步的动作：{label}（{definition.id}）")
        detail.append(f"动作说明：{definition.description or '（没写）'}")
        if definition.llm_level == "command":
            detail.append(f"要触发的指令：{definition.trigger_command or '（没填）'}")
        else:
            tools = "、".join(definition.tool_list())
            detail.append(f"要调用的工具：{tools or '（没选）'}")
        system_prompt = (
            "你在帮一个角色把日程里的一步说清楚：她到点要让某个工具或指令替她做什么。"
            "只输出一句中文，不要解释、不要引号、不要 Markdown。"
        )
        prompt = (
            "\n".join(detail)
            + "\n\n按这个动作的用途写一句「这一步想干什么」，"
            "它会交给模型去填工具 / 指令的参数（例如「看看今天有什么科技新闻」）。"
            "只说这一句，别写参数名。"
        )
        reply = await self._ask_creator(self._generator_session_id(), system_prompt, prompt)
        return " ".join(str(reply or "").split())[:80]

    def _steps_needing_intent(self, chain: list[Any] | None) -> list[tuple[int, ActionDef]]:
        """动作链里哪几步需要意图（工具型 / 指令型，且还没写）。1-based 下标。"""

        action_map = self.world.action_map()
        wanted: list[tuple[int, ActionDef]] = []
        for index, step in enumerate(chain or [], start=1):
            definition = action_map.get(str(getattr(step, "type", "") or ""))
            if definition is None or definition.llm_level not in ("tool", "command"):
                continue
            if str(getattr(step, "intent", "") or "").strip():
                continue
            wanted.append((index, definition))
        return wanted

    async def _smart_chain(
        self, state: WorldState, schedule: Any
    ) -> tuple[list[Any], str]:
        """智能日程：把动作链交给大模型，让它给几步补「想干什么」。

        **动作链本身一个字都不改**（步数、动作 id、时长、台词都照旧），模型只填意图——
        这样就不会出现"配的是查热搜新闻、它却去上网搜索"这种事，也不用它操心怎么走路
        （自动前往由插件补）。
        返回 ``(处理后的动作链, 说明文字)``。
        """

        chain = list(schedule.action_chain or [])
        wanted = self._steps_needing_intent(chain)
        if self.llm is None or not wanted:
            return chain, ""
        now = self.local_now()
        steps = [
            {
                "index": index,
                "label": definition.name or definition.id,
                "action_id": definition.id,
                "kind": "指令型" if definition.llm_level == "command" else "工具型",
                "description": str(definition.description or "").strip(),
            }
            for index, definition in wanted
        ]
        system_prompt, prompt = self.prompts.build_schedule_intent_prompt(
            schedule_id=str(schedule.id),
            when=f"{now.strftime('%Y-%m-%d %H:%M')}（{period_of(now.hour)}）",
            where=self.node_label(state.node_id),
            state_hint=state.mood or "",
            chat_note=self.chat_note_for(state),
            persona=_clip_text(await self._persona_text(state.session_id), 800),
            steps=steps,
            # 「这条日程是干什么的」+ 完整动作链：只给"这一步是什么动作"，
            # 补出来的意图容易跟日程本来的目的对不上
            schedule_note=str(getattr(schedule, "note", "") or ""),
            chain_labels=self._chain_labels(chain),
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_plan(state)
        if reply is None:
            return chain, ""
        intents = self._parse_intents(reply, {index for index, _ in wanted}, len(chain))
        if not intents:
            return chain, ""
        filled: list[Any] = []
        for index, step in enumerate(chain, start=1):
            intent = intents.get(index)
            if intent:
                filled.append(step.model_copy(update={"intent": intent}))
            else:
                filled.append(step)
        action_map = self.world.action_map()
        parts: list[str] = []
        for index in sorted(intents):
            action_id = str(getattr(chain[index - 1], "type", "") or "")
            definition = action_map.get(action_id)
            label = (definition.name or definition.id) if definition else action_id
            parts.append(f"第 {index} 步「{label}」：{intents[index]}")
        return filled, "；".join(parts)

    @staticmethod
    def _parse_intents(reply: str, allowed: set[int], total: int) -> dict[int, str]:
        """从模型返回里挑出「第几步 → 意图」，写得不对的一律丢掉。"""

        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}
        raw = payload.get("intents")
        pairs: list[tuple[Any, Any]] = []
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            for position, item in enumerate(raw, start=1):
                if isinstance(item, dict):
                    pairs.append(
                        (item.get("step", item.get("index", position)), item.get("intent"))
                    )
                else:
                    pairs.append((position, item))
        result: dict[int, str] = {}
        for key, value in pairs:
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            text = " ".join(str(value or "").split())
            if not text or index not in allowed or not (1 <= index <= total):
                continue
            result[index] = text[:80]
        return result

    # ---------------- 日程闸门：日程撞上事件时怎么办 ----------------
    #
    # 只在"这条日程是睡觉 / 小睡 / 换地方"而且她手上确实有件没完的事件时才介入，
    # 所以绝大多数日程照常执行、一次模型都不调。

    GATE_HEAVY_ACTIONS = ("sleep", "nap")
    GATE_MOVING_ACTIONS = ("walk_to",)

    def _schedule_gate_kind(self, schedule: Any) -> str:
        """这条日程要不要过闸门：``heavy``（睡觉类）/ ``moving``（换地方）/ 空串。"""

        kinds = {str(getattr(step, "type", "") or "") for step in list(schedule.action_chain or [])}
        if kinds & set(self.GATE_HEAVY_ACTIONS):
            return "heavy"
        if kinds & set(self.GATE_MOVING_ACTIONS):
            return "moving"
        return ""

    def _schedule_delay_record(self, state: WorldState, schedule_id: str) -> dict[str, Any]:
        record = (state.schedule_delays or {}).get(str(schedule_id))
        return dict(record) if isinstance(record, dict) else {}

    def _schedule_delay_due(
        self,
        state: WorldState,
        schedule: Any,
        day_key: str,
        cursor: float,
        seconds: float,
    ) -> bool:
        """这条日程是不是"推迟到点了"，该再检查一次。"""

        record = self._schedule_delay_record(state, schedule.id)
        if not record or str(record.get("day") or "") != day_key:
            return False
        try:
            until = float(record.get("until") or 0.0)
        except (TypeError, ValueError):
            return False
        return cursor < until <= seconds

    def _delay_slot(self, state: WorldState, schedule: Any) -> str:
        return str(self._schedule_delay_record(state, schedule.id).get("slot") or "")

    def _delay_exhausted(self, state: WorldState, schedule: Any, day_key: str) -> bool:
        """推迟到顶了没有：次数或总时长任一超了就得执行。"""

        record = self._schedule_delay_record(state, schedule.id)
        if str(record.get("day") or "") != day_key:
            return False
        config = self.world.events
        times = max(0, int(getattr(config, "schedule_delay_limit_times", 2) or 0))
        minutes = max(0, int(getattr(config, "schedule_delay_limit_minutes", 180) or 0))
        if times and int(record.get("count") or 0) >= times:
            return True
        if minutes and float(record.get("minutes") or 0.0) >= minutes:
            return True
        return False

    def _delay_schedule(
        self, state: WorldState, schedule: Any, day_key: str, slot: str
    ) -> None:
        """记一次推迟：隔 ``schedule_delay_minutes`` 再检查；睡觉类要付熬夜代价。"""

        config = self.world.events
        minutes = max(5, int(getattr(config, "schedule_delay_minutes", 30) or 30))
        record = self._schedule_delay_record(state, schedule.id)
        if str(record.get("day") or "") != day_key:
            record = {"day": day_key, "count": 0, "minutes": 0.0}
        record["count"] = int(record.get("count") or 0) + 1
        record["minutes"] = float(record.get("minutes") or 0.0) + minutes
        record["until"] = self.local_now().timestamp() + minutes * 60
        record["slot"] = str(slot or record.get("slot") or "")
        state.schedule_delays = {**(state.schedule_delays or {}), str(schedule.id): record}
        if self._schedule_gate_kind(schedule) == "heavy":
            # 熬夜代价：这段时间精力掉得更快
            ticks = max(1, int(minutes * 60 / max(1.0, self.tick_seconds)))
            state.stay_up_until = max(int(state.stay_up_until or 0), state.world_time + ticks)

    def _cancel_schedule(self, state: WorldState, schedule: Any, day_key: str) -> None:
        """今天这条不跑了：记一笔，明天照常。"""

        record = self._schedule_delay_record(state, schedule.id)
        record.update({"day": day_key, "until": 0.0, "cancelled": True})
        state.schedule_delays = {**(state.schedule_delays or {}), str(schedule.id): record}

    async def _schedule_gate(
        self, state: WorldState, schedule: Any, *, label: str
    ) -> tuple[str, str]:
        """日程到点时的闸门：返回 ``("do"|"delay"|"cancel", 说明)``。"""

        config = self.world.events
        if not bool(getattr(config, "schedule_gate", True)):
            return "do", ""
        kind = self._schedule_gate_kind(schedule)
        if not kind:
            return "do", ""  # 伸懒腰、吃饭、看书这类照常做
        thread = self._active_thread(state)
        if thread is None:
            return "do", ""
        day_key = self.local_now().strftime("%Y-%m-%d")
        if self._delay_exhausted(state, schedule, day_key):
            return "do", "推迟到上限了，按日程执行"
        if self._thread_critical(thread):
            # 危险事件：不问模型，直接让路（"被跟踪时不能回卧室睡觉"）
            return "delay", "她手上这件危险的事还没完，先让路"
        choice, reason = await self._ask_schedule_choice(state, schedule, label, thread)
        if choice == "do":
            return "do", ""
        if choice == "cancel":
            return "cancel", f"她自己决定今天不做了（{reason}）"
        return "delay", f"她自己决定先处理手上的事（{reason}）"

    async def _ask_schedule_choice(
        self, state: WorldState, schedule: Any, label: str, thread: dict[str, Any]
    ) -> tuple[str, str]:
        """问主模型：照做 / 推迟 / 今天算了。没有模型时按类型兜底。"""

        kind = self._schedule_gate_kind(schedule)
        fallback = ("delay", "先处理手上的事") if kind == "heavy" else ("do", "")
        if self.llm is None:
            return fallback
        event_line = (
            f"{thread.get('title') or '一件事'}"
            + (f"（第 {len(list(thread.get('steps') or [])) + 1} 轮）" if thread.get("steps") else "")
        )
        system, prompt = self.prompts.build_schedule_gate_prompt(
            persona_text=await self._persona_text(state.session_id),
            schedule_text=label,
            event_line=event_line,
            state_line=self._state_persona_line(state, self.node(state.node_id)),
        )
        raw = await self._ask_llm(state.session_id, system, prompt)
        payload = extract_json_object(raw or "") or {}
        choice = str(payload.get("choice") or "").strip().lower()
        if choice not in ("do", "delay", "cancel"):
            return fallback
        reason = " ".join(str(payload.get("reason") or "").split())[:60]
        return choice, reason

    async def run_schedules(self) -> list[TickOutcome]:
        """检查并触发到点的日程。

        判定不是「当前这一分钟刚好等于配置时间」，而是「游标之后、现在之前」
        这段时间里到点的都算——tick 被拖慢、整分钟被跳过时也能补上。
        """

        outcomes: list[TickOutcome] = []
        if self.schedules is None:
            return outcomes
        once_done: set[tuple[str, str]] = set()
        # 她的一天只过一遍：同一个她（会话组）只检查一次日程
        for session_id in self.tick_session_ids():
            async with self.session_state(session_id) as state:
                now = self.local_now()
                seconds = now.timestamp()
                day_key = now.strftime("%Y-%m-%d")
                self._prune_once_schedules(day_key)
                weekday = self.WEEKDAY_KEYS[now.weekday()]
                candidates = [
                    schedule for schedule in self.schedules.schedules
                    if schedule.enabled
                    and not (schedule.once and schedule.date and schedule.date != day_key)
                    and (schedule.once or not schedule.days or weekday in schedule.days)
                ]
                if self._schedule_reset_pending:
                    cursor = seconds - self.SCHEDULE_LOOKBACK_SECONDS
                else:
                    cursor = self._schedule_cursor(state, now)
                state.schedule_cursor = seconds
                if not candidates:
                    continue
                due: list[tuple[Any, str]] = []
                for schedule in candidates:
                    moment = self._schedule_moment(now, schedule)
                    if moment is None:
                        continue
                    stamp = moment.timestamp()
                    if cursor < stamp <= seconds:
                        due.append((schedule, moment.strftime("%H:%M")))
                    elif self._schedule_delay_due(state, schedule, day_key, cursor, seconds):
                        # 之前被事件推迟的那条：到重试时间了，再走一遍闸门
                        due.append((schedule, str(self._delay_slot(state, schedule) or "")))
                for schedule, slot in sorted(
                    due, key=lambda item: item[0].priority, reverse=True
                ):
                    if not self.schedule_in_scope(schedule, session_id):
                        # 这条日程是别的会话 / 别的组的事
                        continue
                    # 勾了会话组 = 让她自己挑组里的哪一处说（见 resolve_schedule_target）
                    target = await self.resolve_schedule_target(state, schedule, session_id)
                    # 这一轮的允许落点：锁上以后动作模型的 send_to 越不过它
                    locked_targets = self.schedule_lock_targets(schedule, session_id)
                    # 广播目标只看"勾了哪几个具体会话"：勾组 = 她在组里挑一处说，
                    # 不是一次发一片（锁集合比它大：勾组时组里每一处都允许她开口，
                    # 但真正发出去的只有她挑的那一处）
                    broadcast_targets = [
                        item
                        for item in self.schedule_targets(schedule, session_id)
                        if item != target
                    ]
                    label = schedule.id
                    if schedule.action_chain:
                        names = [
                            (self.world.action_map()[step.type].name or step.type)
                            if step.type in self.world.action_map()
                            else step.type
                            for step in schedule.action_chain
                        ]
                        if names:
                            label = f"{schedule.id}（{' → '.join(names)}）"
                    if not self._conditions_ok(state, schedule.conditions):
                        reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": f"schedule:{schedule.id}",
                                "note": f"日程「{label}」到点了却没跑：{reason}",
                            },
                        )
                        continue
                    echo_marker = await self._event_marker(state)
                    claimed = await self.db.call(
                        "mark_schedule_fired",
                        session_id=session_id,
                        schedule_id=schedule.id,
                        day_key=day_key,
                        slot=slot,
                    )
                    if not claimed:
                        continue
                    # 记下"这条日程上次什么时候真的跑了"：提示词里要写给她看
                    last_map = dict(getattr(state, "schedule_last_fired", None) or {})
                    last_map[str(schedule.id)] = float(self._now())
                    state.schedule_last_fired = last_map
                    # 日程闸门：她正忙着处理一件事件时，睡觉/小睡/换地方要先问一句
                    choice, gate_note = await self._schedule_gate(
                        state, schedule, label=label
                    )
                    if choice != "do":
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": f"schedule:{schedule.id}",
                                "note": (
                                    f"日程「{label}」{gate_note}"
                                    if gate_note
                                    else f"日程「{label}」被事件挤开了"
                                ),
                            },
                        )
                        if choice == "delay":
                            self._delay_schedule(state, schedule, day_key, slot)
                        else:  # cancel：今天这条就算了
                            self._cancel_schedule(state, schedule, day_key)
                        continue
                    # 她的日程落在哪个会话里说（勾了私聊就发私聊）
                    outcome = TickOutcome(session_id=target, speech_kind="schedule")
                    outcome.broadcast_targets = broadcast_targets
                    outcome.locked_targets = locked_targets
                    # 这条日程发生在哪儿：调试回显要跟着它走（不然私聊的日程把回显发进群）
                    outcome.place = target
                    chain = schedule.action_chain
                    smart_note = ""
                    if schedule.smart:
                        # 智能日程：让大模型给缺意图的步骤补一句（动作链本身不动）
                        chain, smart_note = await self._smart_chain(state, schedule)
                    if schedule.auto_travel:
                        chain = self.expand_chain_for_travel(state, chain)
                    # 「日程开始执行」要能进日志与调试输出：不然只能看到它的动作，
                    # 不知道这一串是谁安排的。
                    await self._log_event(
                        state,
                        "schedule",
                        {
                            "id": schedule.id,
                            "time": slot,
                            "actions": " → ".join(self._chain_labels(chain)),
                            "manual": False,
                            "smart": bool(schedule.smart),
                            "once": bool(schedule.once),
                            "note": str(schedule.note or ""),
                            "intents": smart_note,
                        },
                    )
                    # 「这条日程是干什么的」+ 完整动作链，一并带进这一轮的上下文：
                    # 她说话时才有背景（不然只知道自己"正在做某个动作"，
                    # 不知道为什么做、后面还要做什么）。
                    chain_labels = self._chain_labels(chain)
                    brief: list[str] = []
                    if str(schedule.note or "").strip():
                        brief.append(f"到点了：这是你之前专门排的一件事——{schedule.note}")
                    if chain_labels:
                        brief.append("这条日程要做的事（按顺序）：" + " → ".join(chain_labels))
                    if brief:
                        state.event_digest = [*list(state.event_digest or []), *brief][-6:]
                    await self._run_chain(state, chain, outcome, depth=0)
                    outcome.notes.append(f"日程 {schedule.id} 已触发")
                    # 做完了这一整串要有交代：她之后提起"我刚做了…"才有据可依
                    if chain_labels:
                        state.event_digest = [
                            *list(state.event_digest or []),
                            "按日程做完了：" + " → ".join(chain_labels),
                        ][-6:]
                    state.add_event(
                        "schedule",
                        {"id": schedule.id, "note": str(schedule.note or "")},
                    )
                    await self._echo_events_since(state, outcome, echo_marker)
                    outcomes.append(outcome)
                    if schedule.once:
                        session = self.session_config(self.config_scope_id)
                        owner = self.config_scope_id if "schedules" in (getattr(session, "config_overrides", {}) or {}) else ""
                        once_done.add((owner, schedule.id))
        # All inheriting sessions must see a shared one-off before it is retired.
        for owner, schedule_id in once_done:
            with self.config_scope(owner):
                self._drop_schedule(schedule_id)
        self._schedule_reset_pending = False
        return outcomes

    @session_scoped
    async def run_schedule_now(
        self, session_id: str, schedule_id: str, *, force: bool = True
    ) -> dict[str, Any]:
        """立刻跑一遍某条日程的动作链（编辑器按钮 / `/mw schedule run` 用）。

        - 不看时间与星期，这是"手动跑一次"，**不占当天的触发名额**，
          到点之后它照常还会正常触发；
        - ``force=True``（默认）连触发条件一起忽略，方便测试；
          传 False 时条件不满足会返回原因，不会硬跑。
        """

        if not self.is_enabled(session_id):
            return {"ok": False, "reason": "这个会话不在白名单里"}
        schedule = next(
            (
                item
                for item in (self.schedules.schedules if self.schedules else [])
                if item.id == schedule_id
            ),
            None,
        )
        if schedule is None:
            return {"ok": False, "reason": f"没有找到日程「{schedule_id}」"}
        if not schedule.enabled and not force:
            return {"ok": False, "reason": f"日程「{schedule.id}」是停用状态"}

        # 手动跑也认这条日程自己的落点（勾了私聊就发私聊；勾了组就让她自己挑一处）
        outlook = await self.load_state(session_id, cold_start=False)
        # 必须与 run_schedules 走同一套落点规则，不然"编辑器里试跑"和"到点真跑"
        # 会落在两个不同的会话里
        locked_targets = self.schedule_lock_targets(schedule, session_id)
        outcome = TickOutcome(
            session_id=await self.resolve_schedule_target(outlook, schedule, session_id),
            speech_kind="schedule",
        )
        outcome.broadcast_targets = [
            item
            for item in self.schedule_targets(schedule, session_id)
            if item != outcome.session_id
        ]
        outcome.locked_targets = locked_targets
        outcome.place = outcome.session_id
        async with self.session_state(session_id) as state:
            if not force and not self._conditions_ok(state, schedule.conditions):
                reason = self._schedule_reason(state, schedule.conditions) or "条件不满足"
                return {
                    "ok": False,
                    "reason": f"触发条件不满足：{reason}",
                    "schedule_id": schedule.id,
                }
            echo_marker = await self._event_marker(state)
            chain = schedule.action_chain
            smart_note = ""
            if schedule.smart:
                # 智能日程手动跑也一样：让大模型给缺意图的步骤补一句
                chain, smart_note = await self._smart_chain(state, schedule)
            if schedule.auto_travel:
                chain = self.expand_chain_for_travel(state, chain)
            # 手动跑一次也把"这条日程是什么 + 动作链"记进她自己的账
            manual_labels = self._chain_labels(chain)
            manual_brief: list[str] = []
            if str(schedule.note or "").strip():
                manual_brief.append(f"你刚跑了一遍日程：{schedule.note}")
            if manual_labels:
                manual_brief.append("这条日程要做的事（按顺序）：" + " → ".join(manual_labels))
            if manual_brief:
                state.event_digest = [*list(state.event_digest or []), *manual_brief][-6:]
            await self._run_chain(state, chain, outcome, depth=0)
            # 手动跑一次也算"上次执行"：编辑器里试跑之后，提示词里就能看到时间
            last_map = dict(getattr(state, "schedule_last_fired", None) or {})
            last_map[str(schedule.id)] = float(self._now())
            state.schedule_last_fired = last_map
            state.add_event(
                "schedule",
                {"id": schedule.id, "manual": True, "note": str(schedule.note or "")},
            )
            await self._log_event(
                state,
                "schedule",
                {
                    "id": schedule.id,
                    "time": schedule.time,
                    "actions": " → ".join(self._chain_labels(chain)),
                    "manual": True,
                    "smart": bool(schedule.smart),
                    "intents": smart_note,
                },
            )
            await self._echo_events_since(state, outcome, echo_marker)
            running = str((state.current_action or {}).get("desc") or "")

        await self._deliver(outcome)
        notes = [str(item) for item in outcome.notes if str(item).strip()]
        if notes:
            summary = "；".join(notes)
        elif outcome.messages:
            summary = "她说：" + " / ".join(outcome.messages)
        elif running:
            summary = f"她现在在做：{running}"
        else:
            summary = "动作链跑完了"
        return {
            "ok": True,
            "schedule_id": schedule.id,
            "messages": list(outcome.messages),
            "notes": notes,
            "note": _clip_text(f"已执行「{schedule.time} {schedule.id}」：{summary}", 160),
        }

    def expand_chain_for_travel(self, state: WorldState, chain: list[Any]) -> list[Any]:
        """给日程的 auto_travel 用：把"需要先在某处"的动作前面自动插一步移动。

        例：日程动作链是 [上网搜索]，开启 auto_travel 后她在卧室时会变成
        [移动到书房, 上网搜索]，这样就不用管理员手写移动步骤。
        """

        graph = self.world.adjacent()
        tracked = state.node_id
        expanded: list[Any] = []
        for step in chain or []:
            action_id = str(getattr(step, "type", "") or "")
            definition = self.world.action_map().get(action_id)
            if action_id == "walk_to":
                target = str(
                    getattr(step, "target_node", "") or getattr(step, "target", "") or ""
                )
                if target in self.world.node_map():
                    tracked = target
                expanded.append(step)
                continue
            if definition is not None:
                wanted: list[str] = []
                if definition.scope == "node":
                    wanted = [
                        node_id
                        for node_id in definition.allowed_nodes
                        if node_id in self.world.node_map()
                    ]
                if wanted and tracked not in wanted:
                    target = nearest_node(graph, tracked, wanted)
                    if target:
                        expanded.append(ChainStep(type="walk_to", target_node=target))
                        tracked = target
            expanded.append(step)
        return expanded

    def _conditions_ok(self, state: WorldState, conditions) -> bool:
        if conditions is None:
            return True
        if conditions.not_state and state.state in conditions.not_state:
            return False
        if conditions.state and state.state not in conditions.state:
            return False
        if conditions.min_energy is not None and state.energy < conditions.min_energy:
            return False
        if conditions.max_energy is not None and state.energy > conditions.max_energy:
            return False
        if conditions.min_loneliness is not None and state.loneliness < conditions.min_loneliness:
            return False
        if conditions.node_in and state.node_id not in conditions.node_in:
            return False
        return True

    # ================= Tick =================
