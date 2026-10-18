"""自主行为：发呆、主动搭话与决策。"""

from __future__ import annotations

from typing import Any
from ..config_scope import session_scoped
from ..json_actions import parse_plan_payload
from ..models import NodeDef
from ..planner import (
    active_plan,
    create_plan,
)
from ..state import STATE_DROWSY, WorldState
from .models import TickOutcome
from .util import _clip_text


class AutonomyMixin:
    # ================= 自主行为 =================

    @session_scoped
    async def maybe_decide(
        self, session_id: str, *, force: bool = False
    ) -> TickOutcome | None:
        """决策器评估：规则优先，必要时（低频抽样）才问 LLM。

        ``force=True``（编辑器里手动点"触发决策"）会跳过间隔节流，
        但仍然尊重"她正在忙 / 在睡觉"这些硬条件——只是不再静默吞掉。
        """

        if not self.is_enabled(session_id):
            return None
        now = self._now()
        # 节流按"这一整个她"算，不按会话：同一个组的几个会话共用一个她
        pace_key = self.state_key(session_id)
        last = self._last_decider_at.get(pace_key, 0.0)
        if not force and now - last < self.decider_interval:
            return None
        self._last_decider_at[pace_key] = now

        outcome = TickOutcome(session_id=session_id)
        outcome.place = session_id
        pushed = False
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            if state.current_action is not None or active_plan(state) is not None:
                return None
            if state.is_sleeping or state.state == STATE_DROWSY:
                return None
            if self.world.sleep.mode == "autonomous" and state.sleep_rhythm.get("decision") == "sleep":
                await self._run_rhythm_sleep(state, outcome)
            elif not self.engagement.can_speak(state):
                outcome.notes.append("处于发言冷却期，继续安静生活")
                await self._quiet_rule_plan(state, node, outcome)
            elif not self._within_hourly_limit(state):
                outcome.notes.append("达到每小时自主行为上限，继续安静生活")
                await self._quiet_rule_plan(state, node, outcome)
            else:
                # 长期低落会关掉「想被注意到」这条动机（带最小关闭时长）
                self._refresh_interject_closed(state)
                # 先看"是不是想他想得不行了"：软推她自己去找他一次（在哪说由她决定）
                pushed = await self._maybe_miss_push(state, node, outcome, session_id)
                if not pushed:
                    # 想被碰一碰：同样是"交给她自己决定"，但要排在"想他"后面
                    pushed = await self._maybe_desire_push(
                        state, node, outcome, session_id
                    )
                if not pushed:
                    await self._decide_normally(state, node, outcome, session_id)
            await self._echo_events_since(state, outcome, echo_marker)
        # 发消息这件事必须在**出锁之后**做：_deliver 自己还要拿这把会话锁
        if outcome.messages or outcome.live_messages or outcome.debug_messages:
            await self._deliver(outcome)
        return outcome

    async def _quiet_rule_plan(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """Speech throttles must not stop sleep, recovery or ordinary movement.

        Quiet provenance follows queued steps and their completion, so an action
        finishing after the cooldown cannot unexpectedly publish its narration.
        """

        plan = self.decider.rule_plan(state, group_chatting=False, interject_allowed=False)
        if plan is None:
            return
        steps = []
        for step in plan.get("steps") or []:
            definition = self.world.action_map().get(str(step.get("action") or ""))
            if definition is None or definition.id in {"say", "share", "poke"}:
                continue
            if definition.llm_level in {"tool", "command"} or step.get("interject"):
                continue
            steps.append({**step, "kind": "quiet"})
        if not steps:
            return
        await self._apply_plan(state, node, outcome, {**plan, "steps": steps})
        await self._tick_plan(state, node, outcome, depth=0)

    async def _decide_normally(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> None:
        """没被软推打断时的常规自主决策（规则优先，低频抽样才问 LLM）。"""

        # 「她想插话但被拦住」按闸门分类计数：单看频率数字没法知道是谁在限流
        gate = self.interject_gate(state)
        wants_interject = self.group_is_chatting(state) and float(
            state.loneliness
        ) >= float(self.world.decider.interject_threshold)
        if wants_interject:
            self._count_interject_gate(state, gate)
        plan = self.decider.rule_plan(
            state,
            group_chatting=self.group_is_chatting(state),
            interject_allowed=gate == "",
        )
        # 要不要交给大模型，由「决策意愿」算出的概率决定；
        # 规则决策不受影响，命中就直接执行。
        if self.decider.should_ask_llm(state):
            llm_plan = await self._ask_llm_for_plan(state, node, outcome)
            plan = llm_plan or plan
        if plan is not None and self.plan_speaks(plan):
            # 刚被搭话、她也回过了：这段时间别主动开口，免得跟被动回复挤在一起
            if self.engagement.proactive_blocked(state):
                outcome.notes.append("刚回过话，这轮不主动搭话")
                await self._log_event(
                    state,
                    "engagement",
                    {"reason": "刚回过话，跳过主动发言"},
                )
                plan = None
        if plan is not None:
            # 她想说给谁：她自己在计划里挑了一个会话就发那儿；没挑就按动机定——
            # 「因为别人在说话所以她想接一句」要落在**那个说话的会话**，
            # 其余情况落在这一次是在哪儿决定的。
            outcome.session_id = self._freeze_plan_target(
                state,
                outcome,
                plan,
                fallback=self._plan_home_for(state, plan, session_id),
            )
            await self._apply_plan(state, node, outcome, plan)
            self._count_autonomous(state)
            await self._tick_plan(state, node, outcome, depth=0)

    async def _apply_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        plan: dict[str, Any],
    ) -> None:
        """把一份计划挂到状态上并记日志（自主决策与「抵达新地点」共用）。"""

        if outcome.speech_kind in {"reply", "schedule", "event"}:
            for step in plan.get("steps", []):
                if isinstance(step, dict) and not step.get("kind"):
                    step["kind"] = outcome.speech_kind
        if any(step.get("interject") for step in plan.get("steps", [])):
            state.last_interject_at = self._now()
        state.current_plan = plan
        # 留一份「最近一次生成的计划」：做完之后提示词里还要能提到它
        state.last_plan = {
            "steps": [
                {
                    "action": str(step.get("action") or ""),
                    "target_node": str(step.get("target_node") or ""),
                }
                for step in plan.get("steps", [])
                if isinstance(step, dict)
            ],
            "reason": str(plan.get("reason") or ""),
            "source": str(plan.get("source") or ""),
            "at": int(state.world_time),
            "send_to": str(plan.get("send_to") or ""),
        }
        if plan.get("reason"):
            state.note_reasoning({"intent": str(plan.get("reason"))}, source="plan")
        # 自主决策时她心里那一句也留下：以前只有 think 动作才写，
        # 模型不主动想，「内心活动」那一栏就长期只有一行旧内容。
        self._remember_inner_thought(
            state,
            str(plan.get("inner") or ""),
            source="自主决定",
            intent=str(plan.get("reason") or ""),
        )
        state.add_event(
            "plan", {"reason": plan.get("reason"), "source": plan.get("source")}
        )
        # 这份计划要说的话落在哪个会话：日志里跟着一起写出来，
        # 免得"决定在私聊"和"话说在群里"看起来对不上
        target = str(plan.get("send_to") or "").strip()
        target_label = self.session_label(target, state) if target else ""
        await self._log_event(
            state,
            "plan",
            {
                "reason": plan.get("reason"),
                "source": plan.get("source"),
                "steps": plan.get("steps"),
                "send_to": target_label,
                "raw": _clip_text(plan.get("raw"), 200),
            },
            outcome=outcome,
        )

    async def decide_after_arrival(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """刚走到一个新地方：立刻看看这儿能做什么。

        「特地走过去」本身就是一次明确的意图，所以这一步直接问大模型，
        不看那条概率抽样、也不占「每小时最多自主行动次数」的额度；
        但仍然守冷却和一个专门的每小时上限，避免来回走 + 反复决策把 token 烧光。
        """

        if not self.world.decider.enabled or state.is_sleeping or state.state == STATE_DROWSY:
            return
        if state.current_plan is not None or state.current_action is not None:
            return
        if not self.engagement.can_speak(state):
            outcome.notes.append("抵达新地点，冷却期内继续安静生活")
            await self._quiet_rule_plan(state, node, outcome)
            return
        if not self._arrival_decision_allowed(state):
            outcome.notes.append("抵达新地点的决策次数已达每小时上限")
            return
        self._count_arrival_decision(state)
        plan = self.decider.rule_plan(
            state,
            group_chatting=self.group_is_chatting(state),
            interject_allowed=self.interject_allowed(state),
        )
        if plan is not None and self.plan_speaks(plan) and self.engagement.proactive_blocked(state):
            outcome.notes.append("刚回过话，到了新地方也不主动搭话")
            plan = None
        # 她特地走过来通常是有目的的：把刚才的打算和她现在能做什么摆在最前面，
        # 免得这次调用还在"重新规划整段时间"。
        where = (node.name if node else "") or state.node_id
        intent = str((state.last_reasoning or {}).get("intent") or "").strip()
        here = "、".join(
            f"{item.id}（{item.name or item.id}）" for item in self.world.actions_in(state.node_id)
        )
        hint = f"你刚刚特地走到了「{where}」"
        hint += f"，因为你打算：{intent}。" if intent else "。"
        hint += (
            f"这里能做：{here or '没什么特别的'}。"
            "直接安排你到了这里要做的那件事；如果你只是随便走走，就挑一件这儿能做的事。"
        )
        llm_plan = await self._ask_llm_for_plan(
            state, node, outcome, force=True, hint=hint
        )
        plan = llm_plan or plan
        if plan is None:
            return
        outcome.notes.append("抵达新地点，就地决定接下来做什么")
        outcome.session_id = self._freeze_plan_target(
            state,
            outcome,
            plan,
            fallback=self._plan_home_for(state, plan, outcome.session_id),
        )
        await self._apply_plan(state, node, outcome, plan)
        await self._tick_plan(state, node, outcome, depth=0)

    async def _ask_llm_for_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        force: bool = False,
        hint: str = "",
        with_extension: bool = True,
    ) -> dict[str, Any] | None:
        if self.llm is None:
            return None
        if not force and not self._llm_plan_allowed(state):
            outcome.notes.append("LLM 计划预算已用完，本轮只用规则决策")
            return None
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        # 配额用尽的动作：计划里既不列出来，也不接受模型写它
        blocked_actions = self._hidden_actions(state)
        if self.world.sleep.mode == "autonomous":
            blocked_actions.add("sleep")
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                automatic=True,
                session_id=state.session_id, persona_id="", node_id=state.node_id, limit=4
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            # 计划轮**没有**明确目标会话（她还没决定说给谁）：故意不传 session_id，
            # 按代表会话取一份窗口、别处的当背景——这就是"全组视图"的语义。
            # 别照抄这条到定向入口上（那些地方必须传目标会话，见 chat_window 的说明）。
            recent_chat=self.chat_window(state),
            mode="plan",
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            hidden_actions=blocked_actions,
            session_directory=self.session_directory(state),
            current_session=state.session_id,
            session_labels=self.session_labels(state),
            profile_text=self.profile_block(state),
            # 这是"她自己决定下一步做什么"的那一轮：好奇心可以推动她去问人
            extra_notes=[
                note for note in [self.extra_reminders(state, autonomy=True)] if note
            ],
            **await self.runtime_notes(state.session_id),
        )
        # 装了扩展（注册了提示词层的那种）就把它的那一层也带上：
        # 她自己决定这一轮做什么的时候，得看得见扩展那几个数值。
        if with_extension:
            layer = self._extension_prompt(state, state.session_id)
            if layer:
                system_prompt = f"{system_prompt}\n\n{layer}"
        note = self.autonomous_prompt_note(state, respect_pause=outcome.speech_kind not in {"reply", "schedule", "event"})
        prompt = (
            (f"{hint}\n\n" if hint else "")
            + ("\n\n" + note + "\n\n" if note else "")
            + "按你自己的节奏决定接下来做什么，输出计划 JSON：\n"
            '{"plan":[{"action":"walk_to","target_node":"window"},'
            '{"action":"stare","duration":600},{"action":"think","content":"..."}],'
            '"valid_until":1800,"inner":"此刻心里的一句话","reason":"为什么这样安排"}\n'
            "计划的长度由你决定：可以是几分钟的小事，也可以睡一整晚"
            "（duration 是这一步持续多少秒；valid_until 是这份计划大概管多少秒）。\n"
            "结合上面的日期时间、你的状态和作息挑一件最合适的事："
            "自主模式的整觉由作息评估接管，不要在普通活动计划里重新安排；固定模式遵循已有作息。"
            "白天只是犯困就先小睡一会儿（10~60 分钟，睡多久你自己定），缓过来接着过，"
            "别在白天一睡八小时——那样醒来正好是半夜。\n"
            + self.prompts.AUTONOMOUS_MANNERS
            + "只输出 JSON。"
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        outcome.llm_calls += 1
        self._count_llm_plan(state)
        if reply is None:
            return None
        plan, _warnings = parse_plan_payload(
            reply,
            available_actions=self._parseable_action_ids(
                state.node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
        )
        if plan:
            created = create_plan(
                steps=plan.get("steps", []),
                world_time=state.world_time,
                valid_for=int(plan.get("valid_for", self.world.limits.plan_valid_duration)),
                reason=str(plan.get("reason", "")),
                source="llm",
                send_to=str(plan.get("send_to") or ""),
            )
            if created is not None:
                # 留下模型原话，日志页里能看到"她为什么这么安排"
                created["raw"] = _clip_text(reply, 240)
            return created
        return None
