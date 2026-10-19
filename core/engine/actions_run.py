"""工具调用落地与动作启动。"""

from __future__ import annotations

from ..model_calls import model_operation

import math
from typing import Any
from ..json_actions import (
    PlannedAction,
    extract_json_object,
    parse_action_payload,
    speakable_messages,
)
from ..memory import INNER
from ..models import (
    ActionDef,
    NodeDef,
    pronoun_for as pronounce_for,
)
from ..pathfinding import (
    find_path,
    path_ticks,
)
from ..planner import (
    active_plan,
    advance,
    create_plan,
)
from ..ports import ToolCallResult
from ..speech_policy import SILENT_FOLLOWUP, speech_origin
from ..state import (
    STATE_DROWSY,
    WorldState,
)
from .models import TickOutcome
from .util import (
    _clip_log,
    _guess_duration_seconds,
    _looks_like_argument_error,
    _to_int_or_default,
    render_param_text,
)


class ActionsRunMixin:
    async def _call_one_tool(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        name: str,
        *,
        params: dict[str, Any],
        previous_results: str = "",
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> tuple[ToolCallResult, dict[str, Any]] | None:
        """准备参数 → 调工具 → 参数报错时再补一次。

        返回 ``(调用结果, 实际参数)``；连必填参数都补不出来时返回 ``None``（跳过这个工具）。
        ``previous_results`` 用于一个动作挂多个工具的场景：把前一个工具的结果给补参模型，
        它才能填出网址、编号这类只有前面才知道的参数。
        """

        intent = str(action.get("intent") or action.get("content") or "")
        if previous_results:
            merged, note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=name,
                params=params,
                previous_results=previous_results,
            )
            if merged:
                params = {**params, **merged}
            missing = self.missing_params_for(name, params)
            if missing:
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"工具「{name}」缺少必填参数 {missing}，这次只调了前面的工具"
                        + (f"（{note}）" if note else ""),
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                return None
        call = await self._call_tool(definition.id, params, state, tool_name=name)
        if not call.ok and _looks_like_argument_error(call.error):
            # 工具作者把参数写成"可选"、实现里却必须要：拿报错再补一次，只重试一次
            retried, note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=name,
                params=params,
                error_hint=str(call.error or ""),
            )
            if retried:
                retry_params = {**params, **retried}
                await self._log_event(
                    state,
                    "tool_call",
                    {
                        "action": definition.id,
                        "tool": name,
                        "params": dict(retry_params),
                        "note": "按报错补了一次参数" + (f"：{note}" if note else ""),
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                again = await self._call_tool(
                    definition.id, retry_params, state, tool_name=name
                )
                await self._log_event(
                    state,
                    "tool_result",
                    {
                        "action": definition.id,
                        "tool": again.tool or name,
                        "ok": bool(again.ok),
                        "result": _clip_log(again.text),
                        "error": again.error,
                    },
                    outcome=outcome,
                    silent=not echo,
                )
                if again.ok or again.text:
                    call = again
                    params = retry_params
        await self._log_event(
            state,
            "tool_call",
            {"action": definition.id, "tool": name, "params": dict(params)},
            outcome=outcome,
            silent=not echo,
        )
        return call, dict(call.params or params)

    async def _log_tool_outcome(
        self,
        state: WorldState,
        definition: ActionDef,
        name: str,
        call: ToolCallResult,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> None:
        """工具返回（或失败）写进日志，失败还会记一笔挫败感。"""

        if not call.ok:
            # 工具没跑成 = 挫败感：这条线上一眼看不见，但确实影响她的心情
            self.dynamics.apply_event(state, "tool_failed", now=self._now())
        state.add_event(
            "tool_result",
            {"action": definition.id, "tool": call.tool or name, "ok": bool(call.ok)},
        )
        await self._log_event(
            state,
            "tool_result",
            {
                "action": definition.id,
                "tool": call.tool or name,
                "ok": bool(call.ok),
                "result": _clip_log(call.text),
                "error": call.error,
            },
            outcome=outcome,
            silent=not echo,
        )

    async def _run_tools_smart(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
        texts: list[str],
        failed: list[str],
        images: list[str],
        used: list[str],
    ) -> None:
        """智能选择：让辅助模型按意图挑一个工具，失败先补参、再换下一个。

        每次重问都会把"刚失败的那个"从候选里摘掉，这样它不会反复选中同一个坏工具。
        """

        remaining = list(names)
        while remaining:
            picked, params = await self._pick_tool_for_action(
                state, definition, action, remaining, outcome=outcome
            )
            if picked not in remaining:
                picked, params = remaining[0], {}
            entry = await self._call_one_tool(
                state,
                definition,
                action,
                picked,
                params=dict(params or {}),
                outcome=outcome,
            )
            if entry is None:
                failed.append(f"{picked}：缺少参数")
                remaining = [item for item in remaining if item != picked]
                continue
            call, sent = entry
            used.append(call.tool or picked)
            if call.ok and call.text:
                texts.append(str(call.text))
            elif not call.ok:
                failed.append(f"{call.tool or picked}：{call.error or '没有返回结果'}")
            images.extend(getattr(call, "image_urls", None) or [])
            attachments = list(getattr(call, "attachments", None) or [])
            landing = self._image_home(outcome, action)
            self._attach_images(outcome, attachments, session_id=landing)
            if attachments:
                self._note_own_image(
                    state,
                    str(action.get("intent") or action.get("content") or definition.name),
                    landing,
                )
            await self._log_tool_outcome(
                state, definition, picked, call, outcome=outcome
            )
            if call.ok and (call.text or images):
                return
            if len(remaining) <= 1:
                return
            # 工具本身不可用才换：参数问题的重试已经在 _call_one_tool 里做过了
            remaining = [item for item in remaining if item != picked]

    @model_operation("judge", "为当前动作选择工具")
    async def _pick_tool_for_action(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """智能选择：一次调用同时挑工具和补参数；挑不出来返回 ("", {})。"""

        if self.helper_llm is None or not names:
            return "", {}
        if not self._tool_param_allowed(state):
            return "", {}
        intent = str(action.get("intent") or action.get("content") or "")
        if not intent:
            intent = self.fallback_intent(definition)
        schemas = self.tool_schemas()
        descriptions = self.available_tools()
        tools = [
            {
                "name": name,
                "description": descriptions.get(name, ""),
                "param_text": render_param_text(schemas.get(name) or {}),
            }
            for name in names
        ]
        system_prompt, prompt = self.prompts.build_tool_choice_prompt(
            tools=tools,
            intent=intent,
            recent_chat=self.chat_context(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return "", {}
        picked = str(payload.get("tool") or "").strip()
        if picked not in names:
            return "", {}
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        await self._log_event(
            state,
            "tool_call",
            {
                "action": definition.id,
                "tool": picked,
                "note": f"智能选择：从 {'、'.join(names)} 里挑了这个",
            },
            outcome=outcome,
        )
        return picked, {
            str(key): value for key, value in params.items() if not str(key).startswith("_")
        }

    async def _start_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        depth: int,
        autonomous: bool,
        *,
        from_plan: bool = False,
        skip_drowsy: bool = False,
        reply_planned: bool = False,
    ) -> bool | None:
        # 这里是所有动作真正开始执行的唯一入口（计划 / 日程 / 自主行为 / 工具后续都走这里），
        # 因此把「能不能做」的校验统一放在这里，避免某条路径绕过检查。
        if definition.id == "sleep":
            target = self.world.sleep_node_id()
            if not self._action_usable_now(definition) or (target and state.node_id != target):
                note = "当前时段不能睡整觉" if not self._action_usable_now(definition) else f"需要先走到睡眠地点 {target}"
                outcome.notes.append(note)
                await self._log_event(state, "skip", {"action": "sleep", "note": note}, outcome=outcome)
                return False
            if autonomous and await self._defer_rhythm_for_event(state, outcome):
                return False
        if (definition.id in {"say", "share", "poke"} or definition.llm_level in {"tool", "command"}) and not self.speech_allowed(
            self._speech_target(state, action, outcome) or outcome.home(), outcome.speech_kind,
        ):
            outcome.notes.append(f"自主搭话已暂停，跳过发言动作 {definition.id}")
            if from_plan and state.current_plan is not None:
                advance(state, status="skipped")
            return False
        blocked = self._extension_gate(definition, state)
        if blocked:
            # 扩展说了不让做（例如"这个动作只能在私聊里做"）：跳过并留痕
            outcome.notes.append(f"动作 {definition.id} 被扩展拦下：{blocked}")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"「{definition.id}」被扩展拦下：{blocked}"},
                outcome=outcome,
            )
            return
        # 扩展想在"这件事真的做起来"时记一笔（返回的话顺口说出去）
        try:
            note = self.extensions.action_note(definition, state, state.session_id)
        except Exception:
            note = ""
        if note:
            outcome.add_speech(note, outcome.home())
        if not definition.available_in(state.node_id) and not (definition.enabled and definition.id == "sleep" and self.world.sleep_node_id() == state.node_id):
            outcome.notes.append(
                f"动作 {definition.id} 在 {state.node_id} 不可用，已跳过"
            )
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"「{definition.id}」在 {state.node_id} 不可用"},
                outcome=outcome,
            )
            return
        if definition.idle:
            outcome.notes.append("闲置动作由空闲轮换执行，不进入正式动作链")
            return
        state.idle_activity = {}
        ok, reason = self._check_preconditions(state, definition)
        if not ok:
            outcome.notes.append(f"动作 {definition.id} 前置条件不满足（{reason}），已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"「{definition.id}」前置条件不满足：{reason}",
                },
                outcome=outcome,
            )
            return
        if self.action_quota_left(state, definition) <= 0:
            # 配额用尽：这一步不做（日志里写清是哪个周期到顶了）
            outcome.notes.append(f"动作 {definition.id} 今天/本周/本月的次数已用完，已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"「{definition.name or definition.id}」的使用次数已达上限",
                },
                outcome=outcome,
            )
            return
        self._count_action_use(state, definition)
        if self._should_drowse(state, definition, skip_drowsy=skip_drowsy):
            # 睡前先进临睡期：不立刻躺下（见 _enter_drowsy）
            await self._enter_drowsy(state, node, outcome, definition, action, depth)
            return
        if definition.id == "sleep" and str((active_plan(state) or {}).get("source")) != "forced":
            # 晚安与临睡期是两个独立开关；直接入睡也保留睡前问候机会。
            await self._maybe_say_goodnight(state, node, outcome)
        if definition.llm_level == "tool":
            if not await self._prepare_tool_action(state, definition, action, outcome):
                return
        if definition.id == "share" and not self._share_allowed(state):
            outcome.notes.append("超过每小时分享上限，跳过分享")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "超过每小时分享上限"},
                outcome=outcome,
            )
            return

        if definition.category == "continuous":
            duration_ticks = self._duration_ticks(definition, action, state)
            if from_plan and (state.current_plan or {}).get("source") == "life_matter" and definition.id != "walk_to":
                duration_ticks = max(1, math.ceil(action.duration / self.tick_seconds))
            if duration_ticks <= 0:
                duration_ticks = 1
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "intent": action.intent,
                "target": action.target,
                "target_node": action.target_node,
                "content": action.content,
                "duration_ticks": duration_ticks,
                "elapsed_ticks": 0,
                "interruptible": bool(definition.interruptible),
                "visible": bool(definition.visible),
                "depth": depth,
                "persona_id": "",
                "from_plan": bool(from_plan),
                # 这个动作是在哪个会话里被安排/触发的：做完的续说回那儿
                "session": outcome.home(),
                "speech_kind": outcome.speech_kind,
                "silent_followup": SILENT_FOLLOWUP.get() or bool(from_plan and (state.current_plan or {}).get("silent_followup")),
                "desc": definition.name or definition.id,
            }
            if from_plan and isinstance(state.current_plan, dict):
                # 记下"这一步属于哪份计划的第几步"：计划中途可能被插进新的安排，
                # 只凭下标认不出自己，做完时就可能把别人的步骤当成自己的推掉
                payload["plan_created_at"] = int(state.current_plan.get("created_at") or 0)
                payload["plan_id"] = state.current_plan.get("id", "")
                payload["plan_index"] = int(state.current_plan.get("current_step") or 0)
                payload["matter_id"] = state.current_plan.get("matter_id", "")
                payload["matter_step_id"] = state.current_plan.get("matter_step_id", "")
            if definition.id == "walk_to":
                target = action.target_node or action.target
                if target in self.world.node_map():
                    graph = self.world.adjacent()
                    path = find_path(graph, state.node_id, target)
                    if path is None:
                        outcome.notes.append(f"无法从 {state.node_id} 走到 {target}")
                        return
                    payload["duration_ticks"] = max(1, path_ticks(graph, path))
                    payload["target_node"] = target
                    payload["desc"] = f"正在走去{self.world.node_map()[target].name}"
                else:
                    outcome.notes.append(f"找不到目标 {target}，移动取消")
                    return
            # 正在做的事被顶掉时留个痕迹：以前是"无声替换"，
            # 日志里既看不到她原本在干什么，心情上也没有任何反应。
            if definition.during.state:
                state.state = definition.during.state
            if state.current_action and str(state.current_action.get("type")) in {"sleep", "nap"}:
                self._finish_sleep_rhythm(state, state.current_action, interrupted=True)
            if definition.id in ("sleep", "nap"):
                self._start_sleep_rhythm(state, definition.id, duration_ticks)
                # 一段新的睡眠：多久、被吵醒过几次、噪音计数都从这里重新算
                state.sleep_started_at = int(state.world_time or 0)
                state.startled_count = 0
                state.startled_until = 0
                state.startled_note = ""
                state.sleep_noise_at = 0.0
                state.sleep_noise_count = 0
                state.sleep_named_count = 0
                # 真的躺下了：临睡期里揣着的那一步就此消化掉。
                # 不清的话它会留到醒来以后，提示词里那句"你困得不行了"会一直挂着
                # （精力满着也整天说困），而且当天再也进不了临睡期。
                state.drowsy_sleep_step = {}
                state.drowsy_started_world_time = 0
                state.drowsy_started_at = 0.0
            previous = state.current_action if isinstance(state.current_action, dict) else None
            if previous:
                self.pause_matter_action(state, previous)
            if previous and str(previous.get("type") or "") != definition.id:
                self.dynamics.apply_event(state, "interrupted", now=self._now())
                state.add_event("interrupt", {"action": previous.get("type")})
                await self._log_event(
                    state,
                    "interrupt",
                    {
                        "action": previous.get("type"),
                        "note": f"她正在做「{previous.get('desc') or previous.get('type')}」，"
                        f"这一步把它顶掉了",
                    },
                    outcome=outcome,
                )
            if previous and previous.get("from_plan"):
                # 被顶掉的那一步别再捡回来接着做：从计划里划掉，
                # 否则新动作做完之后又会回到旧动作，看着像"同一件事做了两遍"
                index = self._running_plan_index(state, state.current_plan)
                if index is not None:
                    advance(state, status="skipped")
                    outcome.notes.append(
                        f"「{previous.get('desc') or previous.get('type')}」被打断，"
                        "计划里那一步不再接着做"
                    )
            state.current_action = payload
            state.add_event("action_start", {"type": definition.id})
            await self._log_event(
                state,
                "action_start",
                {
                    "type": definition.id,
                    "duration_ticks": duration_ticks,
                    "target_node": payload.get("target_node") or "",
                    "params": dict(action.params or {}),
                },
                outcome=outcome,
            )
            # 工具 / 指令型的持续动作：**当场**就把工具、指令跑掉，出图也立刻发出去，
            # 剩下的时间只是占位。不然图要等到持续时间走完才出来，
            # 而"做完再补一句"反而会赶在图片前面。
            if definition.llm_level in ("tool", "command"):
                await self._start_live_call(state, node, outcome, definition, action, payload)
            if (
                definition.llm_level == "template"
                and str(definition.template or "").strip()
                and self._action_speaks(definition)
            ):
                outcome.add_speech(
                    self.render_template(definition.template, state, node, action),
                    self._speech_target(state, action, outcome),
                )
            if definition.id in ("search_web", "check_weather"):
                outcome.notes.append(f"开始 {definition.id}")
            return

        # 瞬时动作
        if definition.id == "recall":
            # 主动回忆：翻记忆 → 带着结果再问她一次（她只需要在续说里讲话）
            await self._run_recall(state, node, outcome, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id in self.SCHEDULE_ACTION_IDS:
            # 日程三件套：同样"做完再问她一次"，她只负责说话
            await self._run_schedule_action(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id == "remember":
            # 当场记住：她判断值得记的事直接写进通讯录（事实 / 关系 / 好感）
            await self._run_remember(state, outcome, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.id == "poke":
            # 戳一戳：能戳就戳（群里什么都没说也看得出来她在闹），戳不了就退化成一句文案
            await self._run_poke(state, node, outcome, definition, action)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "command":
            # 指令触发：拼一条指令交给 AstrBot 跑，再把结果交回给她
            payload = {"type": definition.id, "session": outcome.home(),
                       "silent_followup": SILENT_FOLLOWUP.get() or bool(from_plan and (state.current_plan or {}).get("silent_followup"))}
            await self._run_command_action(
                state, node, outcome, definition, action, remember=payload
            )
            # 指令型动作也能记状态槽（生图插件的「刷新穿搭」走的就是这条路）
            await self._remember_external_state(state, definition, payload, outcome)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return
        if definition.llm_level == "tool":
            # 瞬时工具动作也要真的调工具：当场调用 → 就着结果说一句。
            # from_plan 交回下面统一处理，避免计划被推进两次。
            payload: dict[str, Any] = {
                "type": definition.id,
                "params": dict(action.params),
                "tool_params": dict(getattr(action, "tool_params", {}) or {}),
                "intent": action.intent,
                "target": action.target,
                "content": action.content,
                "queries": list(getattr(action, "queries", []) or []),
                "depth": depth,
                "elapsed_ticks": 0,
                "duration_ticks": 0,
                "from_plan": False,
                "desc": definition.name or definition.id,
                "reply_planned": reply_planned,
                "speech_kind": outcome.speech_kind,
                "silent_followup": SILENT_FOLLOWUP.get() or bool(from_plan and (state.current_plan or {}).get("silent_followup")),
            }
            answered = await self._finish_action(state, node, outcome, payload)
            if from_plan and state.current_plan is not None:
                advance(state)
                await self._tick_plan(state, node, outcome, depth=depth + 1)
            return answered

        messages = await self._instant_output(
            state, node, definition, action, session_id=outcome.home()
        )
        speaker = self._speech_target(state, action, outcome)
        dropped = speaker is None and bool(messages)
        if dropped:
            # 她想去的地方不在名单里（例如「私聊某个没在组里的人」）：
            # 这句不发出去，也不改发到当前会话——免得私下的话当众说出口。
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": (
                        f"她想去「{action.send_to}」说这句，但那儿不在她能说话的地方里，"
                        "这句没说出口"
                    ),
                },
                outcome=outcome,
            )
            outcome.notes.append(
                f"落点「{action.send_to}」不在她能说话的地方里，这句话已丢弃"
            )
        if definition.id == "share":
            if messages and not dropped:
                for text in messages:
                    outcome.add_speech(text, speaker)
                self._count_share(state)
        elif self._action_speaks(definition):
            if not dropped:
                for text in messages:
                    outcome.add_speech(text, speaker)
        elif definition.id == "think" and action.content:
            self._remember_inner_thought(
                state,
                action.content,
                source="她想事情",
            )
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                content=action.content,
                memory_type=INNER,
                emotion=state.mood,
                weight=0.35,
                affect=state.affect,
                valence=state.valence,
            )
        self._apply_life_completion(state, definition)
        self.dynamics.apply_effects(
            state, definition.on_complete.effects, world=self.world, now=self._now()
        )
        # 她主动贴过来这一下：低落时也算一次安抚（不占当天聊天额度）
        await self._maybe_soothe_from_action(state, definition, outcome=outcome)
        # 瞬时动作也走这条完成路径（`_finish_action` 是持续 / 工具动作那条），
        # 所以"亲近一下满足欲求"两边都要接上
        await self._satisfy_desire_from_action(state, definition, outcome)
        state.add_event("action", {"type": definition.id, "visible": definition.visible})
        await self._log_event(
            state,
            "action",
            {
                "type": definition.id,
                "visible": bool(definition.visible),
                "messages": list(messages),
                "content": action.content,
                "target": action.target,
                "params": dict(action.params or {}),
            },
            outcome=outcome,
        )

        # 计划推进
        if from_plan and state.current_plan is not None:
            advance(state)
            await self._tick_plan(state, node, outcome, depth=depth + 1)

    # 「想事情」这类内容型动作：产出的文字是内心活动，永远不发到群里
    INNER_ACTIONS = ("think",)

    def _action_speaks(self, definition: ActionDef) -> bool:
        """这个动作产出的文本要不要发到群里。

        「动作会发到群里」这个开关藏得比较深，新建动作时默认还是关的，于是出现过
        两次同款事故：单轮动作让大模型写的话、模板动作写好的文案，都只进了日志，
        群里什么都看不到。所以判定改成看"这个动作有没有话要说"：

        - 内心活动类（想事情）永远不发，它的产出只进内心活动与记忆；
        - 模板写了文案 → 这句话本来就是给群里看的，发；
        - 单轮动作 → 它的定义就是"让大模型说一句"，发；
        - 目标写着「群 / 某个群友」的 → 也是明确想说话，发；
        - 剩下的（没有文案、纯空间动作）保持静默。
        """

        if definition.visible:
            return True
        if definition.id in self.INNER_ACTIONS:
            return False
        if definition.llm_level == "template":
            return bool(str(definition.template or "").strip())
        if definition.llm_level == "single":
            return True
        return definition.target_type in ("group", "user")

    async def _instant_output(
        self,
        state: WorldState,
        node: NodeDef | None,
        definition: ActionDef,
        action: PlannedAction,
        *,
        session_id: str = "",
    ) -> list[str]:
        """瞬时动作的输出文本。"""

        if definition.id == "say":
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            if action.interject:
                return await self._generate_text_actions(
                    state,
                    node,
                    "群里正在聊上面「最近群里在聊」那些内容。"
                    "如果你确实有话想接，就自然地接一句（不要逐条复述、不要总结、不要点名批评）；"
                    "先看清那几句是谁在对谁说：没点名找你的话，就当他们在互相聊，"
                    "别把别人话里的「你」当成你自己，也别用「主人」这类专属称呼去接；"
                    "如果说不出什么，就返回空的动作列表，保持安静。",
                    session_id=session_id,
                )
            return await self._generate_text_actions(
                state,
                node,
                "你现在想和群里的人说点什么。用一个符合你此刻状态和人设的短句说出来，"
                "自然、口语化、不要解释设定。",
                session_id=session_id,
                # 这一句是她自己想说的：好奇心高的时候可以顺着问一句还不知道的事
                autonomy=True,
            )
        if definition.id == "share":
            if not self._share_allowed(state):
                return []
            if action.messages:
                return list(action.messages)[: self.world.limits.max_messages_per_say]
            return await self._generate_text_actions(
                state,
                node,
                "你想和大家分享一件此刻的小事。用 1~2 条简短自然的消息说出来。",
                session_id=session_id,
            )
        if definition.llm_level == "template":
            text = self.render_template(definition.template, state, node, action)
            return [text] if text else []
        if action.content:
            return [action.content]
        # 「单轮」动作：这一轮没有现成台词（日程、计划里的步骤都没有），
        # 就让大模型按动作语义现写一句——不然只会发一句机械文案。
        if definition.llm_level == "single" and self.llm is not None:
            generated = await self._generate_text_actions(
                state,
                node,
                self._single_action_instruction(state, definition, action),
                session_id=session_id,
            )
            if generated:
                return generated
        # 没配模型、发言额度用完、或者它没说出什么：退化成一句动作文案
        label = definition.name or definition.id
        target_name = self._target_name(state, action.target)
        if action.target and target_name:
            return [
                self.render_template(f"（{{bot}}{label}了 {{user}}）", state, node, action)
            ]
        return [self.render_template(f"（{{bot}}{label}）", state, node, action)]

    def _single_action_instruction(
        self, state: WorldState, definition: ActionDef, action: PlannedAction
    ) -> str:
        """「单轮」动作没现成台词时，给大模型的一句交代。"""

        label = definition.name or definition.id
        who = self._spoken_target(state, action.target)
        private = not self.chat_is_group(str(getattr(state, "session_id", "") or ""))
        lines = [
            f"你刚刚做了「{label}」这个动作。",
            f"这个动作是什么意思：{definition.description or label}。",
        ]
        if action.intent and action.intent.strip() not in ("", label):
            lines.append(f"你当时的想法：{action.intent.strip()}")
        if who:
            lines.append(
                f"对象是{who}——就是正在跟你说话的那个人。"
                if private
                else f"对象是 {who}。"
            )
        lines.append(
            "用你自己的口吻说一句"
            + ("发给他的话" if private else "发到群里的话")
            + "：短、自然、像随口说的，"
            "不要解释设定、不要分点、不要写成在招呼全场。"
        )
        return "\n".join(lines)

    def _spoken_target(self, state: WorldState, target: str) -> str:
        """当着本人的面怎么称呼对方：私聊一律「你」，群里认得出名字才用名字。

        动作文案里的 `{user}` 原来直接填平台昵称，私聊里就变成
        「（她凑过去蹭了蹭astrbot）」——那是**账号名**，不是人；名字还能是伪装的
        （认人只看 id）。所以私聊里换成第二人称，群里照旧用名字、认不出就还是「你」。
        """

        if not self.chat_is_group(str(getattr(state, "session_id", "") or "")):
            return "你"
        return self._target_display(state, target) or "你"

    @staticmethod
    def _target_name(state: WorldState, target: str) -> str:
        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return target

    @staticmethod
    def _target_display(state: WorldState, target: str) -> str:
        """写进文案里的称呼：认得出名字就用名字，认不出就别把 QQ 号写进句子里。"""

        if not target:
            return ""
        record = state.user_presence.get(target)
        if record and record.get("name"):
            return str(record["name"])
        return ""

    async def _run_poke(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """戳一戳：调平台接口戳对方一下；平台不支持就退化成一句动作文案。"""

        target = str(action.target or "").strip()
        if not target:
            # 没给目标：挑一个最近说过话的人，谁在就戳谁
            recent = state.recent_active_users(limit=1)
            target = str(recent[0].get("user_id") or "") if recent else ""

        ok = False
        reason = ""
        route = ""
        poker = getattr(self.messenger, "poke", None)
        if not target:
            reason = "不知道要戳谁（群里还没有人说话）"
        elif not callable(poker):
            reason = "当前发送通道不支持戳一戳"
        else:
            try:
                # 在"她现在说话的地方"戳：私聊里说话就戳私聊，别戳到群里去
                result = await poker(outcome.home(), target)
                ok = bool(getattr(result, "ok", result))
                reason = str(getattr(result, "reason", "") or "")
                route = str(getattr(result, "route", "") or "")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

        if ok:
            outcome.notes.append(f"戳了 {self._target_name(state, target)}")
            await self._log_event(
                state,
                "poke",
                {
                    "target": target,
                    "name": self._target_name(state, target),
                    "ok": True,
                    "route": route,
                },
                outcome=outcome,
            )
        else:
            # 戳不动也要留痕：不然只看到她"变成了固定文案"，不知道是为什么
            outcome.notes.append(f"戳不动 {target}：{reason or '未知原因'}")
            await self._log_event(
                state,
                "poke",
                {
                    "target": target,
                    "name": self._target_name(state, target),
                    "ok": False,
                    "note": reason or "没有可用的戳一戳通道",
                },
                outcome=outcome,
            )
            text = self.render_template(definition.template, state, node, action)
            if text:
                outcome.add_speech(text, self._speech_target(state, action, outcome))
        self.dynamics.apply_effects(
            state, definition.on_complete.effects, world=self.world, now=self._now()
        )
        state.add_event("action", {"type": "poke", "visible": bool(outcome.messages)})
        await self._log_event(
            state,
            "action",
            {
                "type": "poke",
                "target": target,
                "target_name": self._target_name(state, target),
                "ok": ok,
                "note": "戳了一戳" if ok else f"没戳成，改用文案：{reason}",
            },
            outcome=outcome,
        )

    def render_template(
        self,
        text: str,
        state: WorldState,
        node: NodeDef | None,
        action: PlannedAction,
    ) -> str:
        """渲染动作模板里的占位符：{bot} 她自己、{user} 目标群友、{node} 当前地点。"""

        if not text:
            return ""
        bot = (
            (self.world.bot_name or "").strip()
            or state.bot_base_nickname
            or pronounce_for(self.world.gender)
        )
        user = self._spoken_target(state, action.target) or "你"
        result = (
            text.replace("{bot}", bot)
            .replace("{user}", user)
            .replace("{node}", (node.name if node else "") or "")
        )
        return result

    @speech_origin("schedule")
    async def _run_chain(
        self,
        state: WorldState,
        chain: list[Any],
        outcome: TickOutcome,
        *,
        depth: int,
        plan_source: str = "schedule",
    ) -> str:
        """执行日程动作链：瞬时动作立即执行；持续动作启动后，剩余步骤转为计划。"""

        skipped = 0
        completed = 0
        for index, step in enumerate(chain or []):
            action = PlannedAction(
                type=str(getattr(step, "type", "") or ""),
                messages=list(getattr(step, "messages", []) or []),
                target=str(getattr(step, "target", "") or ""),
                target_node=str(getattr(step, "target_node", "") or ""),
                content=str(getattr(step, "content", "") or ""),
                # 这一步"想干什么"必须带过去：工具型动作的参数就是照它补的，
                # 丢了它就只能拿动作说明兜底（以前日程里写的意图等于白写）
                intent=str(getattr(step, "intent", "") or ""),
                params=dict(getattr(step, "params", {}) or {}),
                duration=int(getattr(step, "duration", 0) or 0),
                queries=[
                    str(item) for item in (getattr(step, "queries", []) or []) if str(item)
                ],
                search_depth=str(getattr(step, "search_depth", "") or ""),
                read_pages=_to_int_or_default(getattr(step, "read_pages", None), -1),
            )
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"日程里的动作 {action.type} 不存在，已跳过")
                skipped += 1
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"日程里的动作「{action.type}」不存在"},
                    outcome=outcome,
                )
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作（日程里排到的这一步也跳过）
                outcome.notes.append(f"日程动作 {action.type} 已停用，已跳过")
                skipped += 1
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」已停用",
                    },
                    outcome=outcome,
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(
                    f"日程动作 {action.type} 前置条件不满足（{reason}），已跳过"
                )
                skipped += 1
                # 日程被跳过的原因必须进事件日志，否则日志页看不出"她为什么没做这件事"。
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"日程的这一步「{definition.name or action.type}」前置条件不满足：{reason}",
                    },
                    outcome=outcome,
                )
                continue
            if definition.category == "continuous":
                previous_action = state.current_action
                previous_drowsy = state.drowsy_sleep_step
                await self._start_action(
                    state, self.node(state.node_id), outcome, definition, action, depth, True
                )
                started = state.current_action is not previous_action or (
                    state.state == STATE_DROWSY and state.drowsy_sleep_step is not previous_drowsy)
                if not started:
                    skipped += 1
                    outcome.notes.append(f"日程动作 {action.type} 未启动，继续检查后续步骤")
                    continue
                if state.state == STATE_DROWSY and state.drowsy_sleep_step:
                    state.drowsy_sleep_step["plan_source"] = plan_source
                rest = chain[index + 1 :]
                if rest and (
                    state.current_action is not None or state.state == STATE_DROWSY
                ):
                    # 临睡期也算"手上这件事没做完"：日程后面那几步要等她睡下再继续，
                    # 不能因为还没真躺下就把它们丢掉。
                    remaining = [
                        {
                            "action": str(getattr(item, "type", "") or ""),
                            "target_node": str(getattr(item, "target_node", "") or ""),
                            "target": str(getattr(item, "target", "") or ""),
                            "duration": int(getattr(item, "duration", 0) or 0),
                            "content": str(getattr(item, "content", "") or ""),
                            "intent": str(getattr(item, "intent", "") or ""),
                            "messages": list(getattr(item, "messages", []) or []),
                            "params": dict(getattr(item, "params", {}) or {}),
                        }
                        for item in rest
                    ]
                    state.current_plan = create_plan(
                        steps=remaining,
                        world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                        valid_for=self.world.limits.plan_valid_duration,
                        reason="日程后续步骤",
                        source="schedule",
                    )
                    if state.current_plan and plan_source == "manual":
                        # 保留日程发言来源，单独标记手动试跑供模式交接识别。
                        state.current_plan["manual"] = True
                return "running"
            executed = await self._execute_actions(
                state,
                self.node(state.node_id),
                outcome,
                [action],
                depth=depth,
                autonomous=True,
                from_schedule=True,
                # 地点限制由日程的「自动先走过去」开关决定：没开就是硬条件，
                # 到了别处这一步就跳过，而不是悄悄替她走一趟。
                allow_remote_travel=False,
            )
            if executed:
                completed += 1
            else:
                skipped += 1
        if chain and not skipped:
            # 整条日程顺顺利利跑完：心情上给一点正反馈
            self.dynamics.apply_event(state, "schedule_done", now=self._now())
        if skipped:
            return "partial" if completed else "skipped"
        return "completed" if chain else "skipped"

    @model_operation("action", "当前动作需要生成文字或后续行为")
    async def _generate_text_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        instruction: str,
        *,
        session_id: str = "",
        autonomy: bool = False,
    ) -> list[str]:
        """用 LLM 生成要说的话（say / share 没有现成文案时）。

        ``session_id``：这句话准备说给哪个会话听。**必须带上**——提示词里
        「你能说话的地方」和"这里是哪儿"都按它算；不带的话她会以为只有脚下这一处
        能说话，跨会话的承诺（"我去群里说一声"）就落不了地。

        ``autonomy``：这句话是"她自己想说的"（不是接别人的话、也不是某个动作的台词）。
        只有这种场合才把「好奇心上来了，想去问问谁」带进来，接话和台词都别拐弯。

        生成不出来时**一律返回空**（这一轮她保持安静）。以前这里会退到一句
        写死的通用短句——那句话跟谁的人设都不搭，说错话比不说伤害大得多。
        """

        if self.llm is None:
            return []
        if not self._llm_text_allowed(state):
            self._log("debug", "自主发言的 LLM 预算用完了，这一轮保持安静")
            return []
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        current = str(session_id or state.session_id or "")
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            # 这一轮是要说给 current（定向会话）听的：窗口也必须按它取，
            # 否则目标会话的话可能只落进"别处听到的"，本会话的上下文反而是代表会话的。
            recent_chat=self.chat_window(state, current),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            profile_text=self.profile_block(state),
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=autonomy)] if note
            ],
            session_directory=self.session_directory(state),
            current_session=current,
            session_labels=self.session_labels(state),
            hidden_actions=self.exhausted_actions(state),
            samples=self.voice_sample_lines(state, state.session_id),
            **await self.runtime_notes(state.session_id),
        )
        # Sending is gated by the caller; scheduled text generation must remain available.
        note = self.autonomous_prompt_note(state, respect_pause=False)
        prompt = (
            instruction
            + ("\n\n" + note if note else "")
            + "\n\n"
            + self.prompts.AUTONOMOUS_MANNERS
            + '\n只输出 JSON：{"actions":[{"type":"say","messages":["..."]}]}'
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_text(state)
        if not reply:
            return []
        result = parse_action_payload(
            reply,
            available_actions={"say"},
            max_actions=1,
            max_messages=say_limit,
        )
        for item in result.actions:
            if item.type == "say" and item.messages:
                return item.messages
        cleaned = reply.strip()
        if not cleaned:
            return []
        if cleaned.lstrip().startswith("{"):
            # 模型又回了一段 JSON、但里面没有能说的话（例如 actions 是空的 = 她不想开口）：
            # **绝不能**把整段 JSON（连 reasoning 一起）当成台词发出去
            payload = extract_json_object(cleaned)
            spoken = speakable_messages(payload) if isinstance(payload, dict) else []
            if spoken:
                return spoken[:say_limit]
            self._log("debug", "主动发言这一轮模型给的是 JSON 且没有要说的话，按保持安静处理")
            return []
        return [cleaned]

    def _duration_ticks(
        self, definition: ActionDef, action: PlannedAction, state: WorldState
    ) -> int:
        """把动作时长换算成 tick。

        - ``duration_mode=fixed``：用配置的固定秒数（大模型给的 duration 不生效）；
        - ``duration_mode=llm``：用大模型给的秒数，并夹在 duration_min~duration_max 之间
          （大模型没给就看她的 intent / 台词里有没有说多久，都没有才用下限——
          「小睡三小时」这种话写在 intent 里、duration 忘了填是最常见的写法）。
        """

        if definition.id == "sleep" and self.world.sleep.mode == "autonomous":
            low, high = self._rhythm_duration_bounds()
            seconds = min(high, max(low, float(action.duration or definition.duration or 28800)))
        elif definition.duration_mode == "llm":
            low = int(definition.duration_min or 60)
            high = int(definition.duration_max or max(low, int(definition.duration or low * 4)))
            if high < low:
                high = low
            seconds = int(action.duration or 0)
            if not seconds:
                for hint in (action.intent, *list(action.messages or [])):
                    seconds = _guess_duration_seconds(hint)
                    if seconds:
                        break
            seconds = seconds or low
            seconds = min(max(seconds, low), high)
        else:
            seconds = action.duration or definition.duration or 0
        return max(1, int(round(seconds / max(1.0, self.tick_seconds))))

    async def _call_tool(
        self,
        action_id: str,
        params: dict[str, Any],
        state: WorldState,
        tool_name: str = "",
    ) -> ToolCallResult:
        """调用工具，返回结构化结果（成功文本 / 失败原因）。"""

        if self.tools is None:
            return ToolCallResult(ok=False, error="没有可用的工具通道")
        chosen = self.resolve_tool(action_id, state.node_id, tool_name)
        if not chosen:
            return ToolCallResult(ok=False, error="没有解析到可用工具")
        try:
            result = await self.tools.call_tool(
                chosen, dict(params or {}), state.session_id
            )
        except Exception as exc:
            self._log("warning", f"工具 {chosen} 调用失败: {exc}")
            self.note_tool_result(chosen, ok=False, error=str(exc))
            return ToolCallResult(ok=False, error=str(exc), tool=chosen)
        if isinstance(result, ToolCallResult):
            if not result.tool:
                result.tool = chosen
            if not result.params:
                result.params = dict(params or {})
            # 成功就清掉失败计数；"工具坏了"的失败才计入熔断
            self.note_tool_result(
                chosen, ok=bool(result.ok), error=str(result.error or "")
            )
            return result
        # 兼容返回纯字符串的实现
        text = str(result or "").strip()
        if not text:
            self.note_tool_result(chosen, ok=False, error="工具返回了空结果")
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=chosen, params=dict(params or {})
            )
        self.note_tool_result(chosen, ok=True)
        return ToolCallResult(ok=True, text=text, tool=chosen, params=dict(params or {}))

    async def _ask_llm(
        self,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict[str, Any]] | None = None,
        image_urls: list[str] | None = None,
    ) -> str | None:
        def note(ok: bool, error: str = "") -> None:
            self._last_llm[session_id] = {
                "at": self._now(),
                "ok": bool(ok),
                "error": str(error)[:200],
            }

        if self.llm is None:
            note(False, "没有可用的模型")
            return None
        try:
            reply = await self._background_generate(self.llm,
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=list(image_urls) if image_urls else None,
            )
        except Exception as exc:
            self._log("warning", f"LLM 调用失败: {exc}")
            note(False, f"{type(exc).__name__}: {exc}")
            return None
        if reply is None or not getattr(reply, "ok", True):
            note(False, getattr(reply, "error", "") or "模型没有返回内容")
            return None
        note(True)
        return getattr(reply, "text", "") or ""

    async def _persona_text(self, session_id: str) -> str:
        if self.persona is None:
            return ""
        try:
            return await self.persona.get_persona_text(session_id)
        except Exception:
            return ""
