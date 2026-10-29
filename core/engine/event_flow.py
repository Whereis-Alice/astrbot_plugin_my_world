"""事件里她先去做点什么，再把结果带回来。"""

from __future__ import annotations

from ..model_calls import model_operation
from .life_inference import locked_life_work

from typing import Any
from ..events import (
    Decision,
    EventPackage,
    MODE_IMAGINED,
    TIER_BIG,
    TIER_MICRO,
    default_decision,
    event_from_payload,
    open_threads,
    parse_suggestions,
)
from ..json_actions import (
    PlannedAction,
    extract_json_object,
)
from ..memory import SCENE
from ..models import (
    ActionDef,
    NodeDef,
)
from ..state import WorldState
from .models import (
    MessageContext,
    ReplyOutcome,
    TickOutcome,
)
from .util import (
    _clip_log,
    _clip_text,
)
from ..speech_policy import SILENT_FOLLOWUP


class EventFlowMixin:
    # ---------------- 事件里她可以先去做点什么 ----------------

    def _event_action_budget(self, state: WorldState, thread: dict[str, Any]) -> int:
        """这条线索还剩几次「先做点什么」的额度。"""

        total = max(0, int(getattr(self.world.events, "event_action_calls", 2) or 0))
        used = max(0, int(thread.get("action_calls") or 0))
        return max(0, total - used)

    def _event_action_defs(
        self, state: WorldState, thread: dict[str, Any]
    ) -> list[ActionDef]:
        """事件里允许她调用的动作。

        默认口径：工具型 / 指令型（能借外部能力做事），排除「往群里发东西」那类；
        每个动作还能用 ``event_usable`` 覆盖（强制允许 / 禁止）。
        """

        if self._event_action_budget(state, thread) <= 0:
            return []
        node_id = state.node_id
        known_tools = set(self.available_tools())
        picked: list[ActionDef] = []
        for action in self.world.actions:
            if not action.enabled:
                continue
            rule = str(getattr(action, "event_usable", "auto") or "auto")
            if rule == "deny":
                continue
            if rule != "allow":
                if action.llm_level not in ("tool", "command"):
                    continue
                if str(getattr(action, "target_type", "none")) == "group":
                    continue
            # 只列真的跑得起来的：工具动作得挂了已注册的工具，指令动作得有指令名
            if action.llm_level == "tool" and not (
                set(action.tool_list()) & known_tools
            ):
                continue
            if action.llm_level == "command" and not str(
                getattr(action, "trigger_command", "") or ""
            ).strip():
                continue
            if not self._action_usable_now(action):
                continue
            if not action.available_in(node_id) and not self._nearest_node_with_action(
                node_id, action
            ):
                continue
            picked.append(action)
        picked.sort(key=lambda item: (int(item.priority or 5), item.id))
        return picked[:12]

    def event_action_lines(self, defs: list[ActionDef]) -> str:
        """动作清单进事件提示词的样子：只有 id 和一句说明，不带参数定义。"""

        lines: list[str] = []
        for action in defs:
            note = " ".join(str(action.description or action.name or "").split())
            lines.append(f"- {action.id}：{_clip_text(note, 60) or '（没有说明）'}")
        return "\n".join(lines)

    def _event_observation_text(self, thread: dict[str, Any]) -> str:
        """已经查到的结果，交给下一轮判断。"""

        items = [str(item).strip() for item in list(thread.get("observations") or [])]
        items = [item for item in items if item]
        return "\n".join(f"- {_clip_text(item, 200)}" for item in items[-6:])

    @locked_life_work
    async def _run_event_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
    ) -> None:
        """执行她在事件里挑的动作，把结果写进「已经查到的」里。

        这一轮严格不说话：动作结果只用来帮她判断这件事怎么办，
        要说的话留到拿主意那一轮（提示词里也是这么约定的）。
        """

        item = dict(decision.actions[0])
        action_id = str(item.get("type") or "")
        definition = self.world.action_map().get(action_id)
        if definition is None or not definition.enabled:
            thread["observations"] = [
                *list(thread.get("observations") or []),
                f"想用「{action_id}」，但这个动作现在用不了",
            ][-6:]
            return
        planned = PlannedAction(
            type=definition.id,
            intent=str(item.get("intent") or ""),
            content=str(item.get("intent") or ""),
            params=dict(item.get("params") or {}),
        )
        marker = await self._latest_event_id(state.session_id)
        previous_action, previous_plan = state.current_action, state.current_plan
        token = SILENT_FOLLOWUP.set(True)
        try:
            await self._execute_actions(
                state, node, outcome, [planned], depth=0, autonomous=True
            )
        finally:
            SILENT_FOLLOWUP.reset(token)
        if state.current_plan and state.current_plan is not previous_plan:
            state.current_plan["silent_followup"] = True
        thread["action_calls"] = int(thread.get("action_calls") or 0) + 1
        results = await self._action_results_since(state.session_id, marker)
        if not results:
            if state.current_action and state.current_action is not previous_action:
                results = ["（动作进行中，尚未取得完成结果；不能据此认定已经做成）"]
            elif state.current_plan and state.current_plan is not previous_plan:
                results = ["（已排入计划，尚未完成；不能据此认定已经做成）"]
            else:
                results = ["（没有取得可核对的结果，不能认定已经做成）"]
        observations = [f"{definition.name or definition.id}：{text}" for text in results]
        thread["observations"] = [
            *list(thread.get("observations") or []),
            *observations,
        ][-6:]
        await self._log_event(
            state,
            "event_action",
            {
                "title": package.title,
                "action": definition.id,
                "intent": str(item.get("intent") or ""),
                "result": _clip_log("；".join(results)),
            },
            outcome=outcome,
        )

    async def _latest_event_id(self, session_id: str) -> int:
        """当前最大事件 id（当作"这次动作之前"的记号）。"""

        try:
            rows = await self.db.call("query_events", session_id=session_id, limit=1)
        except Exception:
            return 0
        return int((list(rows or [{}]) or [{}])[0].get("id") or 0)

    async def _action_results_since(self, session_id: str, marker: int) -> list[str]:
        """这次动作产生的结果文本（工具型和指令型都从日志里取）。"""

        try:
            rows = await self.db.call("query_events", session_id=session_id, limit=40)
        except Exception:
            return []
        texts: list[str] = []
        for row in reversed(list(rows or [])):
            if int(row.get("id") or 0) <= int(marker):
                continue
            kind = str(row.get("event_type") or "")
            detail = row.get("detail") or {}
            if kind in ("tool_result", "command_result"):
                body = str(detail.get("result") or "").strip()
                if not bool(detail.get("ok", True)):
                    body = f"没做成（{detail.get('error') or '没有返回内容'}）"
                if body:
                    texts.append(body)
            elif kind == "skip":
                note = str(detail.get("note") or "").strip()
                if note:
                    texts.append(f"这一步没做成：{note}")
        return [_clip_text(text, 400) for text in texts[-4:]]

    def _followup_package(self, thread: dict[str, Any], hint: str) -> EventPackage:
        """没有模型时，把上一句伏笔直接当成下一步事件。"""

        text = " ".join(str(hint or "").split())
        title = str(thread.get("title") or "")
        return event_from_payload(
            {
                "title": title or text[:16],
                "hook": text,
                "tier": "small",
                "difficulty": 0.5,
                "options": [
                    {"desc": "接着处理", "abilities": ["composure"]},
                    {"desc": "换个办法", "abilities": ["wits"]},
                    {"desc": "先放着", "no_check": True},
                ],
            },
            source="pool",
        ) or EventPackage(title=title or text[:16], hook=text, tier="small")

    async def _continue_thread(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        now: float,
        *,
        thread_id: str = "",
    ) -> bool:
        """有线索的下一步到点了：接着演（续线时把整条线索喂回去）。

        ``thread_id`` 只给编辑器上的「立即推进一幕」用：那一次只推这一条，
        不能顺手把别的到点线索也演了。
        """

        config = self.world.events
        for thread in open_threads(self._event_threads(state)):
            if thread_id and str(thread.get("id") or "") != thread_id:
                continue
            followup = str(thread.get("pending_followup") or "").strip()
            when = float(thread.get("next_step_at") or 0.0)
            if not followup or when <= 0 or float(now) < when:
                continue
            if self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
                continue
            # 挂太久了就不再续演：直接收尾，写一条"那件事后来没再提"
            resume_hours = max(0.0, float(getattr(config, "thread_resume_max_hours", 24.0) or 0.0))
            idle = float(now) - float(thread.get("updated_at") or thread.get("opened_at") or now)
            if resume_hours and idle > resume_hours * 3600.0:
                thread["status"] = "closed"
                thread["closed_at"] = float(now)
                thread["pending_followup"] = ""
                thread["next_step_at"] = 0.0
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id="",
                    node_id=str(thread.get("place") or state.node_id),
                    content=f"（{thread.get('title') or '一件事'}）那件事后来没再提",
                    memory_type=SCENE,
                    source="life_event",
                    context=self._event_memory_context(thread),
                    emotion=state.mood,
                    weight=0.25,
                    affect=state.affect,
                    valence=state.valence,
                )
                await self._log_event(
                    state,
                    "event_idle",
                    {"title": thread.get("title"), "stage": "expired"},
                    outcome=outcome,
                )
                continue
            base = event_from_payload(thread.get("root") or {}, source="pool")
            tier = str((base.tier if base else "") or "small")
            self._apply_thread_target(state, outcome, thread)
            package = await self._generate_event_package(
                state, node, tier=tier, thread=thread
            )
            if package is None:
                package = self._followup_package(thread, followup)
            # 这一步是"接着上一步演"，来源标成线索续演（日志里一眼能区分）
            package.source = "follow"
            thread["pending_followup"] = ""
            thread["next_step_at"] = 0.0
            await self._log_event(
                state,
                "event",
                {
                    "title": package.title,
                    "hook": package.hook,
                    "node": state.node_id,
                    "tier": package.tier,
                    "kind": package.kind,
                    "imagined": package.mode == MODE_IMAGINED,
                    "need_help": package.needs_help,
                    "source": package.source or "follow",
                    "tone": str(thread.get("tone") or ""),
                    "thread": thread.get("id"),
                    "step": len(list(thread.get("steps") or [])) + 1,
                },
                outcome=outcome,
            )
            if package.tier == TIER_MICRO or not package.options:
                await self._settle_micro(state, outcome, thread, package)
            else:
                # 分集续演：她这会儿人可能在别处，这一幕按"想起来/复盘"来演，
                # 不硬把场景搬回去（也没法搬——外部场景不在世界地图上）
                await self._run_event_step(
                    state,
                    node,
                    outcome,
                    thread,
                    package,
                    recap=self._needs_recap(state, thread),
                )
            return True
        return False

    def _needs_recap(self, state: WorldState, thread: dict[str, Any]) -> bool:
        """这一幕要不要写成"想起来"：她此刻不在事件发生的那个地方。"""

        place = str(thread.get("place") or "")
        if not place:
            return False
        if place.startswith("outside:"):
            return True
        return place != state.node_id

    def _event_share_mode(self, tier: str) -> str:
        """这一档事件要不要出声（``silent`` / ``nodes`` / ``always``）。"""

        config = self.world.events
        name = str(tier or "small").strip().lower()
        if name not in ("micro", "small", "big"):
            name = "small"
        value = str(getattr(config, f"share_{name}", "") or "").strip().lower()
        return value if value in ("silent", "nodes", "always") else "nodes"

    def _event_step_no(self, thread: dict[str, Any] | None) -> int:
        """这是这条线索的第几幕（从 1 开始；还没记进去的那一幕算下一幕）。"""

        try:
            done = len([item for item in list((thread or {}).get("steps") or []) if item])
        except TypeError:
            done = 0
        return done + 1

    def _event_share_allows(
        self,
        state: WorldState,
        *,
        tier: str,
        thread: dict[str, Any] | None = None,
        followup: str = "",
        ask_for_help: bool = False,
    ) -> bool:
        """这一幕她能不能把话说到群里。

        分档的用意：事件是"她身上发生的事"，不是"要演给群友看的节目"。
        大部分幕只在记忆和状态里过一遍，只有起头、收尾这种真正的节点才说出来——
        这样既不会一个人没头没尾地尬演，日后聊天时又有材料可以当谈资。
        """

        if not self._event_public_speech(state):
            return False
        if ask_for_help:
            # 求助是独立的一档：真要人帮忙才开口，不归分享策略管
            return True
        mode = self._event_share_mode(tier)
        if mode == "always":
            return True
        if mode == "silent":
            return False
        # nodes：只在第一幕和收尾那一幕说
        step = self._event_step_no(thread)
        if step <= 1:
            return True
        return not str(followup or "").strip()

    def _event_must_continue(self, thread: dict[str, Any] | None, *, tier: str) -> bool:
        """这一幕之后必须还有下一幕吗（大事件不许一步收尾）。"""

        if str(tier or "").strip().lower() != TIER_BIG:
            return False
        floor = max(1, int(getattr(self.world.events, "big_min_steps", 1) or 1))
        return self._event_step_no(thread) < floor

    def _note_event_in_chat(
        self,
        state: WorldState,
        package: EventPackage,
        settlement: Any,
        thread: dict[str, Any],
        *,
        followup: str,
    ) -> None:
        """把这件事的结果写进聊天留档——不是她说的话，是"她身上发生的事"。

        只在收尾那一幕写一条，写的是整条线索的累积摘要。这样她日后聊天时
        可以自然提起"我前两天把锅盖拧死了"，而不是每件事都只活在提示词的另一段里。
        """

        if not bool(getattr(self.world.events, "event_into_chat_log", True)):
            return
        if str(followup or "").strip():
            # 还没完：等收尾那一幕再写，免得留档里全是半截话
            return
        line = str(thread.get("line") or "").strip() or str(
            getattr(settlement, "outcome", "") or ""
        ).strip()
        if not line:
            return
        _key, place_name = self._event_place(package, state)
        where = f"·{place_name}" if place_name else ""
        state.note_chat(
            user_id="",
            name="（她自己）",
            text=f"〔她自己身上 #{package.title}{where}〕{line}",
            now=self._now(),
            keep=self.chat_history_limit(),
            internal=True,
            origin=state.session_id,
        )

    def _apply_thread_target(
        self, state: WorldState, outcome: TickOutcome, thread: dict[str, Any] | None
    ) -> None:
        """把"这件事她想说给谁听"落到这一轮的落点上。

        第一幕在 :meth:`_run_event_step` 里已经落过一次；**后面几幕是新的 tick**，
        默认落点是那个 tick 所在的会话——如果她当初选了别的会话（会话组里的另一个），
        这里得再对一次，否则"我在私聊里跟他说这件事"会跑到群里去。
        """

        info = dict((thread or {}).get("decision") or {})
        raw = str(info.get("send_to") or (thread or {}).get("session") or "").strip()
        if not raw:
            return
        picked = self.resolve_send_to(state, raw, fallback="")
        if picked:
            outcome.session_id = picked
            outcome.speech_home = picked

    @model_operation("event", "未结束的事件需要提醒")
    async def _compose_remind_line(
        self,
        state: WorldState,
        *,
        title: str,
        hook: str,
        asked: str,
    ) -> str:
        """求助没人接时她那句自我圆场——让她自己按人设写，写不出来就不说。"""

        channel = self.helper_llm
        if channel is None:
            return ""
        try:
            system, prompt = self.prompts.build_help_remind_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=title,
                hook=hook,
                asked=asked,
            )
        except Exception:
            return ""
        prompt += "\n" + await self.task_background(state, task="expression", query=title + "\n" + hook + "\n" + asked)
        try:
            reply = await channel.generate(
                session_id=state.session_id,
                system_prompt=system,
                prompt=prompt,
                temperature=0.4,
            )
        except Exception as exc:
            self._log("debug", f"生成求助圆场那句失败：{exc}")
            return ""
        self._count_tool_param(state)
        text = getattr(reply, "text", "") if getattr(reply, "ok", False) else ""
        payload = extract_json_object(str(text or ""))
        line = ""
        if isinstance(payload, dict):
            line = " ".join(str(payload.get("line") or "").split())
        elif str(text or "").strip():
            line = " ".join(str(text).split())
        # 只要一句：模型偶尔会顺手写两行，后面的丢掉
        line = line.splitlines()[0].strip() if line else ""
        if line.startswith("{"):
            return ""
        return line[:40]

    async def _advance_pending_help(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome, now: float
    ) -> None:
        """等待中的求助：活跃 →（轻提醒）轻等待 →（超时）自己收尾。"""

        info = dict(self._pending_event_help(state))
        stage = str(info.get("state") or "")
        if stage not in ("active", "idle"):
            return
        config = self.world.events
        thread = self._thread_by_id(state, str(info.get("thread_id") or ""))
        if stage == "active":
            if float(now) < float(info.get("active_until") or 0.0):
                return
            info["state"] = "idle"
            info["reminder_sent"] = True
            info["idle_until"] = float(info.get("asked_at") or now) + max(
                1.0, float(getattr(config, "idle_minutes", 60.0) or 60.0)
            ) * 60.0
            self._set_event_help(state, info)
            root = dict((thread or {}).get("root") or {})
            text = ""
            if bool(getattr(config, "remind_when_ignored", True)):
                text = await self._compose_remind_line(
                    state,
                    title=str(info.get("title") or root.get("title") or "那件事"),
                    hook=str(root.get("hook") or ""),
                    asked=str(info.get("asked") or ""),
                )
            # 这句话该说给哪个会话：她当初挑了别的会话就跟着她
            self._apply_thread_target(state, outcome, thread)
            if text and self._event_public_speech(state):
                outcome.add_speech(text, kind="event")
            elif text:
                outcome.notes.append("她在睡觉，这句提醒没有发到群里")
            else:
                outcome.notes.append("没人接话，她自己圆场的那句没写出来（这一轮不说）")
            await self._log_event(
                state,
                "help",
                {
                    "title": info.get("title"),
                    "stage": "remind",
                    "text": text,
                    "note": "" if text else "没写出圆场的话，保持安静",
                },
                outcome=outcome,
            )
            return
        if float(now) < float(info.get("idle_until") or 0.0):
            return
        self._set_event_help(state, {})
        if thread is None:
            return
        package = event_from_payload(thread.get("root") or {}, source="pool")
        decision = self._cached_decision(thread)
        if package is None:
            thread["status"] = "closed"
            return
        if decision is None:
            decision = default_decision(package)
        # 收尾这一句也要回到她当初选的那个会话，别落在 tick 所在的组代表会话上
        self._apply_thread_target(state, outcome, thread)
        await self._log_event(
            state,
            "help",
            {
                "title": package.title,
                "stage": "timeout",
                "note": "等不到人，她按自己的判断收尾",
            },
            outcome=outcome,
        )
        await self._log_event(
            state,
            "event_idle",
            {"title": package.title, "stage": "abandon"},
            outcome=outcome,
        )
        await self._resolve_event_step(
            state, node, outcome, thread, package, decision, suggestions=None
        )

    @model_operation("judge", "筛选对方给事件的建议")
    async def _filter_suggestions(
        self,
        state: WorldState,
        package: EventPackage,
        decision: Decision,
        messages: list[str],
    ):
        """群友的回应分拣：只丢无关的，起哄照留。"""

        config = self.world.events
        if not bool(getattr(config, "suggest_tools", True)):
            return None
        system, prompt = self.prompts.build_suggestion_filter_prompt(
            title=package.title,
            hook=package.hook,
            her_plan=decision.desc or "",
            messages=messages,
            red_lines=list(getattr(config, "red_lines", []) or []),
        )
        raw = await self._ask_judge(state.session_id, system, prompt)
        self._count_tool_param(state)
        return parse_suggestions(raw or "")

    async def _consume_help_reply(self, ctx: MessageContext) -> ReplyOutcome | None:
        """她正在等协助时，群友的回应先给事件线看一眼：能消化就消化，否则照常回复。"""

        if self.llm is None:
            return None
        async with self.session_state(ctx.session_id) as state:
            info = self._pending_event_help(state)
            if str(info.get("state") or "") not in ("active", "idle"):
                return None
            if str(info.get("session") or state.session_id) != ctx.session_id:
                return None
            thread = self._thread_by_id(state, str(info.get("thread_id") or ""))
            package = event_from_payload((thread or {}).get("root") or {}, source="pool")
            if thread is None or package is None:
                self._set_event_help(state, {})
                return None
            decision = self._cached_decision(thread) or default_decision(package)
            messages = [f"{ctx.user_name or '有人'}：{ctx.text}"]
            suggestions = await self._filter_suggestions(state, package, decision, messages)
            if suggestions is None or not suggestions.related:
                return None
            # 有人对这件事说了话：事件线把这一轮吃掉，她不再走普通回复
            self._set_event_help(state, {})
            node = self.node(state.node_id) or self.node(self.default_node_id())
            outcome = TickOutcome(session_id=ctx.session_id)
            outcome.place = ctx.session_id
            await self._log_event(
                state,
                "help",
                {
                    "title": package.title,
                    "stage": "reply",
                    # 原文 + 保留条数：只写"摘要"时，全被丢掉就会剩一条空记录
                    "text": _clip_log(" / ".join(messages)),
                    "kept": len(list(getattr(suggestions, "related", []) or [])),
                    "dropped": len(list(getattr(suggestions, "irrelevant", []) or [])),
                    "digest": suggestions.digest(),
                },
                outcome=outcome,
            )
            await self._log_event(
                state,
                "help",
                {
                    "title": package.title,
                    "stage": "resolve",
                    "note": "她把建议听进去了，重新拿主意",
                },
                outcome=outcome,
            )
            await self._run_event_step(
                state,
                node,
                outcome,
                thread,
                package,
                second_round=True,
                suggestions=suggestions,
            )
            return ReplyOutcome(
                ok=True,
                messages=list(outcome.messages),
                debug_messages=list(outcome.debug_messages),
                debug_positions=list(outcome.debug_positions),
            )
