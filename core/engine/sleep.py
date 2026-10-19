"""睡眠：门禁、迷糊惊醒、临睡期与早晚安。"""

from __future__ import annotations

from dataclasses import asdict
from ..config_scope import session_scoped
from ..json_actions import PlannedAction
from ..models import (
    ActionDef,
    NodeDef,
)
from ..planner import active_plan
from ..prompt import period_of
from ..speech_policy import speech_origin
from ..state import (
    STATE_DROWSY,
    STATE_IDLE,
    STATE_NAPPING,
    STATE_SLEEPING,
    WorldState,
)
from .models import (
    MessageContext,
    SleepReply,
    TickOutcome,
)
from .util import _signed


class SleepMixin:
    # ---------------- 睡觉时的门禁 ----------------

    def _is_asleep(self, state: WorldState) -> bool:
        """她现在算不算「睡着了」（小睡按配置决定要不要一起算）。"""

        if state.state == STATE_NAPPING:
            return bool(self.world.sleep.applies_to_nap)
        return state.state == STATE_SLEEPING

    def is_asleep(self, state: WorldState) -> bool:
        """对外暴露的「她睡着了吗」（联动方用，例如意图路由直接不判断）。"""

        return self._is_asleep(state)

    def _wake_hit(self, ctx: MessageContext) -> bool:
        """这条消息算不算「明确叫她起来」。"""

        words = [
            str(word).strip()
            for word in (self.world.sleep.wake_words or [])
            if str(word).strip()
        ]
        if not words:
            return False
        if self.world.sleep.wake_requires_mention and not (ctx.is_mentioned or ctx.is_private):
            return False  # 没 @ 她的闲聊不能把她弄醒
        text = ctx.text or ""
        return any(word in text for word in words)

    def _wake_note_text(self, ctx: MessageContext) -> str:
        """被叫醒时给提示词的一句话。

        重点是别让她"只顾着醒"：同一条消息里交代的事也要安排成动作。
        """

        note = (
            "你刚被叫醒，可以迷糊一点；但对方如果在同一条消息里交代了要做的事，"
            "先把那件事写成动作，再回一句话。"
        )
        if self._wake_leftover(ctx.text or ""):
            note += "对方不只是叫你起来，还交代了事情。"
        return note

    def _wake_leftover(self, text: str) -> str:
        """去掉唤醒词和标点之后，这条消息还剩什么（用来判断是不是还交代了事）。"""

        result = text or ""
        for word in self.world.sleep.wake_words or []:
            word = str(word).strip()
            if word:
                result = result.replace(word, "")
        cleaned = "".join(
            char for char in result if not char.isspace() and char not in "，。！？、,.!?~～…:：;；"
        )
        return cleaned

    @staticmethod
    def _take_wake_note(state: WorldState) -> str:
        """取走「刚被叫醒」这句提示（取走即失效，避免每轮都提）。"""

        if not state.wake_note or state.world_time > state.wake_note_until:
            state.wake_note = ""
            return ""
        note = state.wake_note
        state.wake_note = ""
        return note

    def _sleep_reply_text(self, state: WorldState, ctx: MessageContext) -> str:
        text = str(self.world.sleep.reply_text or "").strip()
        if not text:
            return ""
        return self.render_template(
            text,
            state,
            self.node(state.node_id),
            PlannedAction(type="say", target=ctx.user_id),
        ).strip()

    def _sleep_reply_on_cooldown(self, state: WorldState) -> bool:
        minutes = max(0, int(self.world.sleep.reply_cooldown_minutes))
        if minutes <= 0 or not state.sleep_reply_at:
            return False
        return (self._now() - float(state.sleep_reply_at)) < minutes * 60

    # ---------------- 迷糊惊醒 ----------------

    def _startled_config(self):
        return getattr(self.world.sleep, "startled", None)

    def _startled_active(self, state: WorldState) -> bool:
        """现在是不是在"被吵醒的迷糊窗口"里（这段时间她还算睡着）。"""

        return int(state.startled_until or 0) > int(state.world_time or 0)

    def _note_sleep_noise(self, state: WorldState, ctx: MessageContext) -> None:
        """记一笔"睡着时被吵"：滑动窗口里的条数，以及有几次是冲她来的。"""

        config = self._startled_config()
        if config is None or not bool(getattr(config, "enabled", True)):
            return
        now = self._now()
        window = max(60.0, float(getattr(config, "window_minutes", 5) or 5) * 60)
        if not state.sleep_noise_at or now - float(state.sleep_noise_at) > window:
            state.sleep_noise_at = now
            state.sleep_noise_count = 0
            state.sleep_named_count = 0
        state.sleep_noise_count = int(state.sleep_noise_count or 0) + 1
        named = bool(ctx.is_mentioned or ctx.is_private)
        if not named:
            # 没 @ 她，但喊了她的名字也算冲她来的
            named = self._named_at_her(state, ctx.text or "")
        if named:
            state.sleep_named_count = int(state.sleep_named_count or 0) + 1

    def _named_at_her(self, state: WorldState, text: str) -> bool:
        """她自己的名字出现在消息里没有（用来判断这条是不是冲她来的）。"""

        body = text or ""
        names = [
            str(name)
            for name in (
                self.world.bot_name,
                state.bot_base_nickname,
            )
            if str(name or "").strip()
        ]
        return any(name in body for name in names)

    def _startled_threshold(self, state: WorldState) -> int:
        config = self._startled_config()
        if config is None:
            return 0
        if str(state.state) == STATE_NAPPING:
            return max(1, int(getattr(config, "noise_threshold_nap", 5) or 5))
        return max(1, int(getattr(config, "noise_threshold", 8) or 8))

    async def _maybe_startle_wake(
        self, state: WorldState, ctx: MessageContext
    ) -> bool:
        """吵到位了就"迷糊惊醒"一次；返回 True 表示这条消息放行让她回。"""

        config = self._startled_config()
        if config is None or not bool(getattr(config, "enabled", True)):
            return False
        if not self._is_asleep(state):
            return False
        if self._startled_active(state):
            # 已经在窗口里：只放"冲她来的"那几条过去，别的照旧挡着——
            # 刷屏的人不该因为她在半睡半醒就把每一句都塞给她
            return bool(
                ctx.is_mentioned
                or ctx.is_private
                or self._named_at_her(state, ctx.text or "")
            )
        max_times = max(0, int(getattr(config, "max_per_sleep", 1) or 0))
        if int(state.startled_count or 0) >= max_times:
            return False
        # 刚睡着那一段不惊醒：那时候最沉，也正撞上第一次记忆整理
        started = int(state.sleep_started_at or 0)
        skip_ticks = max(0, int(getattr(config, "skip_first_minutes", 30) or 0)) * 60
        if started and skip_ticks:
            elapsed = max(0, int(state.world_time or 0) - started) * int(self.tick_seconds)
            if elapsed < skip_ticks:
                return False
        named = int(state.sleep_named_count or 0)
        threshold = self._startled_threshold(state)
        if named >= max(1, int(getattr(config, "named_threshold", 2) or 2)):
            reason = f"被连着叫了 {named} 次"
        elif int(state.sleep_noise_count or 0) >= threshold:
            reason = f"群里 {int(state.sleep_noise_count)} 条消息吵得厉害"
        else:
            return False
        await self._startle(state, ctx, reason=reason)
        return True

    async def _startle(self, state: WorldState, ctx: MessageContext, *, reason: str) -> None:
        """把"被吵醒"落到状态上：**不打断睡眠**，只开一个能说话的窗口。"""

        config = self._startled_config()
        quality = getattr(self.world.sleep, "quality", None)
        awake_ticks = max(1, int(getattr(config, "awake_minutes", 3) or 3)) * 60
        state.startled_until = int(state.world_time) + max(
            1, int(round(awake_ticks / max(1.0, float(self.tick_seconds))))
        )
        state.startled_count = int(state.startled_count or 0) + 1
        state.sleep_noise_count = 0
        state.sleep_named_count = 0
        state.sleep_noise_at = 0.0
        penalty = abs(float(getattr(config, "energy_penalty", 0.05) or 0.0))
        if penalty:
            state.energy = max(0.0, float(state.energy) - penalty)
        self._set_grumpy(
            state,
            minutes=int(getattr(config, "grumpy_minutes", 15) or 0),
            valence=float(getattr(config, "grumpy_valence", -0.05) or 0.0),
            affect=float(getattr(config, "grumpy_affect", 0.06) or 0.0),
            note="你刚被吵醒，还在起床气里",
        )
        state.startled_note = (
            "你刚才被吵醒了，现在是半睡半醒：只用「说话」回一两句，"
            "语气迷糊、可以带点起床气，偶尔打错一两个字也正常；"
            "**不要安排任何别的动作**，回完就接着睡，别把话题铺开。"
        )
        state.add_event("startled", {"by": ctx.user_id, "reason": reason})
        await self._log_event(
            state,
            "startled",
            {
                "user": ctx.user_name or ctx.user_id or "有人",
                "reason": reason,
                "awake_minutes": int(getattr(config, "awake_minutes", 3) or 3),
                "energy_penalty": penalty,
            },
        )
        self._log("info", f"[my_world] 她在睡觉，被吵醒了一次（{reason}）")

    def _set_grumpy(
        self,
        state: WorldState,
        *,
        minutes: int,
        valence: float,
        affect: float,
        note: str,
    ) -> None:
        """挂一段起床气：调效价 / 心潮，并在提示词里说清她这会儿是什么状态。"""

        if minutes <= 0:
            return
        ticks = max(1, int(round(minutes * 60 / max(1.0, float(self.tick_seconds)))))
        state.grumpy_until = max(
            int(state.grumpy_until or 0), int(state.world_time) + ticks
        )
        state.grumpy_note = note
        self.dynamics.apply_effects(
            state,
            {"valence": _signed(valence), "affect": _signed(affect)},
            world=self.world,
            now=self._now(),
        )

    def sleep_notes(self, state: WorldState) -> list[str]:
        """睡着被吵醒 / 起床气这几句临时说明（提示词用，过期自动失效）。"""

        notes: list[str] = []
        if self.world.sleep.mode == "autonomous":
            decision = state.sleep_rhythm.get("decision")
            reason = str(state.sleep_rhythm.get("reason") or "")[:180] if (
                decision in {"sleep", "sleeping", "continue"}
                and not state.sleep_delayed and state.world_time >= state.no_sleep_until
            ) else ""
            notes.append("你按自己的疲劳、睡眠记录和安排决定作息，不因到固定钟点就必须睡。" +
                         (f"最近的决定：{reason}。" if reason else ""))
        # 「等会儿再睡」的延迟窗口：他说了再待一会儿，她也答应了——
        # 这段时间别催他、别提睡觉；已经困了的话就是"半睡档"：软、慢、迷糊，但不睡过去。
        delayed = state.sleep_delayed
        delay_left = self.sleep_delay_minutes(state) if delayed else 0
        # 只有真的在临睡期才说"困得不行"：只看那一步揣没揣着会出大问题——
        # 她要是从别的路躺下了（被动回合里她自己说要睡），揣着的那一步没人清，
        # 这句提示就会一直挂着，精力满着也会一天到晚说"我好困"。
        if str(getattr(state, "state", "")) == STATE_DROWSY and not state.is_sleeping:
            if delayed:
                # 半睡档：她困得不行，但答应了再待一会儿，所以只是软下来，不许真睡
                notes.append(
                    f"你已经困得不行了，但他刚说了「等会儿再睡」，你答应了再陪他一会儿"
                    f"（大约还有 {delay_left} 分钟）：**说话短、慢、软，断断续续的**，"
                    "可以含含糊糊地撒娇、赖着不走；**别提睡觉、别催他、也别自己睡过去**。"
                )
            else:
                # 临睡期：她已经困得不行了，只是还没躺下
                notes.append(
                    "你现在困得不行了（正要睡）：**说话短、断断续续、迷迷糊糊的**——"
                    "可以只说半句、只说一个词、或者说到一半就没下文；别安排事情、别长篇解释，"
                    "也别主动挑话题。"
                )
        elif delayed:
            notes.append(
                f"他刚说了「等会儿再睡」，你答应了再待一会儿（大约还有 {delay_left} 分钟）："
                "这段时间**别催他、别主动提睡觉**，也不用急着安排去睡的事。"
            )
        if self._startled_active(state) and state.startled_note:
            notes.append(state.startled_note)
        elif int(state.startled_until or 0) and int(state.startled_until) <= int(
            state.world_time or 0
        ):
            state.startled_note = ""
        if int(state.grumpy_until or 0) > int(state.world_time or 0):
            note = state.grumpy_note or "你现在有点起床气"
            notes.append(
                f"{note}：说话短一点、耐心少一点，别主动撒娇示好，也别急着安排大事。"
            )
        elif state.grumpy_note:
            state.grumpy_note = ""
        return notes

    # ---------------- 临睡期与晚安 / 早安 ----------------

    def _should_drowse(self, state: WorldState, definition: ActionDef, *, skip_drowsy: bool = False) -> bool:
        """这一步是不是"夜里睡前"——要不要先进入临睡期。

        只有**睡整觉**（不是小睡）走这一套，而且一天最多一次（回笼觉就直接躺）。
        极端保护的强制补觉不拖：那是她已经透支了。
        """

        if skip_drowsy or definition.id != "sleep":
            return False
        if not bool(getattr(self.world.sleep, "drowsy", True)):
            return False
        if str((active_plan(state) or {}).get("source") or "") == "forced":
            return False
        if state.state == STATE_DROWSY or state.drowsy_sleep_step:
            return False
        return str(state.drowsy_day or "") != self._today_key(self._now())

    async def _enter_drowsy(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        depth: int,
    ) -> None:
        """进入临睡期：把"真的睡"那一步先揣着，先迷糊一会儿。

        这一段她还在（能回消息、会说晚安），等安静下来才真的躺下。
        """

        now = self._now()
        payload = (
            action.model_dump(mode="json")
            if hasattr(action, "model_dump")
            else asdict(action)
        )
        state.drowsy_sleep_step = dict(payload or {})
        state.drowsy_sleep_step["speech_kind"] = outcome.speech_kind
        plan = active_plan(state) or {}
        state.drowsy_sleep_step["plan_source"] = "manual" if plan.get("manual") else str(plan.get("source") or "")
        state.drowsy_started_world_time = int(state.world_time or 0)
        state.drowsy_started_at = float(now)
        state.drowsy_day = self._today_key(now)
        self.pause_matter_action(state, state.current_action or {})
        state.current_action = None
        state.state = STATE_DROWSY
        state.pending_arrival = False
        state.add_event("drowsy", {"action": definition.id})
        await self._log_event(
            state,
            "drowsy",
            {
                "action": definition.id,
                "note": (
                    f"困了，先临睡一会儿（安静 {int(getattr(self.world.sleep, 'drowsy_minutes', 5) or 5)} "
                    f"分钟、最多 {int(getattr(self.world.sleep, 'drowsy_max_minutes', 30) or 30)} 分钟后去睡）"
                ),
            },
            outcome=outcome,
        )
        # 睡前这一段：要不要说声晚安，由她自己定（发给谁、发不发都行）
        await self._maybe_say_goodnight(state, node, outcome)

    async def _drowsy_tick(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """临睡期的一拍：够安静了（或者拖太久了）就真的去睡。"""

        if not state.drowsy_sleep_step:
            # 没揣着那一步（老存档 / 中途被清）：就当它结束了
            state.state = STATE_IDLE
            state.drowsy_started_world_time = 0
            state.drowsy_started_at = 0.0
            return
        if state.sleep_delayed:
            # 「等会儿再睡」的延迟窗口：就这么挂着——人已经软下来了（临睡期的语气还在），
            # 但不许真睡过去，不然外面的互动（接触、开场景）一条都进不来。
            # 窗口一过，下面那两条判断照旧：安静够了 / 拖太久了，就真的去睡。
            return
        config = self.world.sleep
        quiet_minutes = max(1, int(getattr(config, "drowsy_minutes", 5) or 5))
        max_minutes = max(quiet_minutes, int(getattr(config, "drowsy_max_minutes", 30) or 30))
        ticks = max(1, int(round(max_minutes * 60 / max(1.0, self.tick_seconds))))
        elapsed = max(0, int(state.world_time or 0) - int(state.drowsy_started_world_time or 0))
        # "安静了多久"从进临睡期的这一刻算起，不能被更早的那次发言提前满足
        base = max(float(state.drowsy_started_at or 0.0), float(state.last_user_activity_at or 0.0))
        idle_seconds = self._now() - base
        if elapsed >= ticks:
            await self._go_to_sleep(state, node, outcome, reason="拖太久了，去睡吧")
            return
        if idle_seconds >= quiet_minutes * 60:
            await self._go_to_sleep(state, node, outcome, reason="安静下来了，去睡吧")

    @speech_origin("sleep")
    async def _go_to_sleep(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome, *, reason: str
    ) -> None:
        """临睡期结束：把揣着的那一步睡觉真的执行掉。"""

        if await self._defer_rhythm_for_event(state, outcome):
            return
        step = dict(state.drowsy_sleep_step or {})
        state.drowsy_sleep_step = {}
        state.drowsy_started_world_time = 0
        state.drowsy_started_at = 0.0
        state.state = STATE_IDLE
        definition = self.world.action_map().get("sleep")
        if not step or definition is None:
            return
        outcome.notes.append(reason)
        # 存的是 PlannedAction 的字段（``type``），这里换回计划步骤的写法（``action``）
        action = self._step_to_action({**step, "action": step.get("type") or "sleep"})
        # 这一步已经在临睡期里"等过了"：直接睡（skip_drowsy）
        await self._execute_actions(
            state,
            node,
            outcome,
            [action],
            depth=0,
            autonomous=True,
            from_plan=False,
            skip_drowsy=True,
        )

    async def _maybe_say_goodnight(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> bool:
        """睡前一次机会：要不要说晚安、发给谁——**由她自己决定**，也可以不发。"""

        if not self.speech_allowed(outcome.home(), outcome.speech_kind):
            return False
        if not bool(getattr(self.world.sleep, "goodnight", True)) or self.llm is None:
            return False
        if str(state.goodnight_day or "") == self._today_key(self._now()):
            return False
        now = self.local_now()
        hint = (
            f"这会儿你困得不行，正准备睡（现在是 {now.strftime('%H:%M')}，{period_of(now.hour)}）。\n"
            "**要不要说声晚安，由你自己决定**：\n"
            "- 想发就写一条 `say`，写清 `send_to`——发给谁、发到哪个会话都看你："
            "想单独跟谁道晚安就发他的私聊，想让大家都看到就发群里，"
            "也可以分别给几个人各发一条；\n"
            "- **不想发就什么都不写**（返回空 actions）：今天没人理你、你懒得开口、"
            "或者白天刚聊过，都可以不说，不会被记成「没礼貌」；\n"
            "- 要说就说得短一点、迷迷糊糊的（「睡了啊…明天再聊」「困死了，先躺了」），"
            "别写小作文，也别挨个点名问候。\n"
            "你手边能说话的地方见上面的「你能说话的地方」。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint)
        if plan is None or not self.plan_speaks(plan):
            # 她决定不说了：记一笔，今天不再问
            state.goodnight_day = self._today_key(self._now())
            await self._log_event(state, "goodnight", {"said": False}, outcome=outcome)
            return False
        state.goodnight_day = self._today_key(self._now())
        said = await self._run_sleep_greeting(state, node, outcome, plan)
        await self._log_event(
            state,
            "goodnight",
            {"said": said, "note": str(plan.get("reason") or "")[:80]},
            outcome=outcome,
        )
        return said

    async def _maybe_say_goodmorning(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> bool:
        """睡醒后一次机会：要不要说早安、发给谁——同样由她自己决定。"""

        if not self.speech_allowed(outcome.home(), outcome.speech_kind):
            return False
        if not bool(getattr(self.world.sleep, "goodmorning", True)) or self.llm is None:
            return False
        if str(state.goodmorning_day or "") == self._today_key(self._now()):
            return False
        state.goodmorning_day = self._today_key(self._now())
        now = self.local_now()
        hint = (
            f"你刚睡醒（现在是 {now.strftime('%H:%M')}，{period_of(now.hour)}）。\n"
            "**要不要说声早安，由你自己决定**：\n"
            "- 想发就写一条 `say` 并写清 `send_to`：单独跟谁说就发私聊，想让大家都看到就发群里；\n"
            "- **不想发就什么都不写**（返回空 actions）——刚醒还没缓过来、或者昨天没人接你的话，"
            "都可以不主动冒头；\n"
            "- 要说就短一点、带着刚醒的迷糊（「早…」「醒了，有点渴」），别写成问候模板。\n"
            "你手边能说话的地方见上面的「你能说话的地方」。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint)
        if plan is None or not self.plan_speaks(plan):
            await self._log_event(state, "goodmorning", {"said": False}, outcome=outcome)
            return False
        said = await self._run_sleep_greeting(state, node, outcome, plan)
        await self._log_event(
            state,
            "goodmorning",
            {"said": said, "note": str(plan.get("reason") or "")[:80]},
            outcome=outcome,
        )
        return said

    async def _run_sleep_greeting(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome, plan: dict
    ) -> bool:
        """只执行这一刻的问候，保留睡眠状态与睡醒后排队的安排。"""

        definition = self.world.action_map().get("say")
        if (definition is None or not definition.enabled or definition.category != "instant"
                or definition.llm_level in {"tool", "command"}):
            return False
        steps = [step for step in plan.get("steps", [])
                 if step.get("action") == "say" and step.get("messages")]
        if not steps:
            return False
        greeting = {**plan, "steps": steps[:self.world.limits.max_actions_per_message]}
        self._freeze_plan_target(state, outcome, greeting, fallback=state.session_id)
        before = len(outcome.said_messages()) + sum(len(items) for items in outcome.routed.values())
        previous_home = outcome.speech_home
        try:
            for step in greeting["steps"]:
                outcome.speech_home = str(step.get("session") or previous_home or outcome.session_id)
                await self._start_action(state, node, outcome, definition,
                    self._step_to_action(step), 0, True, from_plan=False)
        finally:
            outcome.speech_home = previous_home
        after = len(outcome.said_messages()) + sum(len(items) for items in outcome.routed.values())
        return after > before

    def _apply_wake_quality(
        self, state: WorldState, *, slept_minutes: float, startled: int = 0
    ) -> None:
        """睡整觉结束时的结算：睡饱 -> 清爽；没睡够 / 被吵过 -> 起床气。"""

        config = getattr(self.world.sleep, "quality", None)
        if config is None or not bool(getattr(config, "enabled", True)):
            return
        full = max(1, int(getattr(config, "full_minutes", 360) or 360))
        if slept_minutes >= full and not startled:
            self.dynamics.apply_effects(
                state,
                {
                    "valence": _signed(
                        float(getattr(config, "rested_valence", 0.08) or 0.0)
                    ),
                    "affect": _signed(
                        float(getattr(config, "rested_affect", -0.05) or 0.0)
                    ),
                },
                world=self.world,
                now=self._now(),
            )
            return
        if slept_minutes >= full:
            # 睡够了但中途被吵醒：算不上起床气，清爽也打个折
            self.dynamics.apply_effects(
                state,
                {
                    "valence": _signed(
                        round(
                            float(getattr(config, "rested_valence", 0.08) or 0.0) / 3, 4
                        )
                    ),
                    "affect": _signed(
                        round(
                            abs(float(getattr(config, "rested_affect", -0.05) or 0.0)) / 2,
                            4,
                        )
                    ),
                },
                world=self.world,
                now=self._now(),
            )
            self._set_grumpy(
                state,
                minutes=int(getattr(config, "grumpy_minutes", 30) or 0),
                valence=0.0,
                affect=0.0,
                note="你睡够了，但中途被吵醒过，还有点没缓过来",
            )
            return
        self._set_grumpy(
            state,
            minutes=int(getattr(config, "grumpy_minutes", 30) or 0),
            valence=float(getattr(config, "grumpy_valence", -0.12) or 0.0),
            affect=float(getattr(config, "grumpy_affect", 0.10) or 0.0),
            note="你没睡够，这会儿有起床气",
        )

    def sleep_delay_minutes(self, state: WorldState) -> int:
        """「等会儿再睡」还剩多少分钟（不在窗口里就是 0）。"""

        if not state.sleep_delayed:
            return 0
        left = int(state.sleep_delay_until) - int(state.world_time)
        return max(0, int(left * self.tick_seconds // 60))

    def delay_sleep(
        self,
        state: WorldState,
        *,
        minutes: int | float | None = None,
        reason: str = "",
        by: str = "",
    ) -> int:
        """把「她要睡了」往后推一个窗口：返回新的 ``sleep_delay_until``（世界时间 tick）。

        - ``minutes=None`` 用配置里的 ``sleep.delay_minutes``（默认 20 分钟）；
        - 连着说要续期：取"更晚的那个"，不会把已有的窗口缩短；
        - 窗口里规则不排睡觉、日程与 LLM 排的睡觉会被跳过、提示词不催她睡，
          她照样可以是迷迷糊糊的临睡状态（``STATE_DROWSY``）——但不会真的睡过去，
          所以外面的互动（接触、开场景）照常生效；
        - ``reason`` / ``by`` 只用来显示与排查（面板、提示词里说明是谁推迟的）。
        """

        config = self.world.sleep
        try:
            wait = int(minutes) if minutes is not None else int(getattr(config, "delay_minutes", 20) or 20)
        except (TypeError, ValueError):
            wait = int(getattr(config, "delay_minutes", 20) or 20)
        if wait <= 0:
            return int(state.sleep_delay_until or 0)
        ticks = max(1, int(round(wait * 60 / max(1.0, self.tick_seconds))))
        state.sleep_delay_until = max(
            int(state.sleep_delay_until or 0), int(state.world_time) + ticks
        )
        if str(reason or ""):
            state.sleep_delay_reason = str(reason)
        if str(by or ""):
            state.sleep_delay_by = str(by)
        state.add_event(
            "sleep_delay",
            {"minutes": wait, "until": int(state.sleep_delay_until), "reason": str(reason or ""), "by": str(by or "")},
        )
        return int(state.sleep_delay_until)

    def relieve_sleep_pressure(
        self, state: WorldState, amount: int | float | None = None, *, reason: str = ""
    ) -> float:
        """把困意压下去一点（有人跟她说话、被碰一碰、亲密接触）。

        亲密场景优先：他还在跟她待着的时候，困意不该自己涨到阈值把她送进睡眠。
        返回压完之后的困意。
        """

        config = self.world.sleep
        if amount is None:
            try:
                amount = float(getattr(config, "pressure_chat_relief", 0.05) or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
        try:
            delta = max(0.0, float(amount))
        except (TypeError, ValueError):
            delta = 0.0
        if not delta:
            return float(state.sleep_pressure or 0.0)
        state.sleep_pressure = max(0.0, min(1.0, float(state.sleep_pressure or 0.0) - delta))
        return float(state.sleep_pressure)

    def _sleep_delay_hit(self, ctx: MessageContext, state: WorldState) -> str:
        """这句话是不是「等会儿再睡」：命中返回那个词，否则返回 ""。"""

        if self._is_asleep(state):
            # 已经睡着了就没有"再等会儿"可言了——那是叫醒的事，走 wake_words
            return ""
        if self.is_plugin_command(ctx.text):
            return ""
        config = self.world.sleep
        if bool(getattr(config, "delay_requires_mention", True)) and not (
            ctx.is_mentioned or ctx.is_private
        ):
            return ""
        text = str(ctx.text or "")
        if not text:
            return ""
        for word in tuple(getattr(config, "delay_words", ()) or ()):
            key = str(word or "").strip()
            if key and key in text:
                return key
        return ""

    def _wake_up_state(self, state: WorldState, ctx: MessageContext) -> None:
        """把「被叫醒」落到状态上：停下动作、可选地清掉排队计划、给一段保护期。"""

        config = self.world.sleep
        if state.current_action and str(state.current_action.get("type")) in {"sleep", "nap"}:
            self._finish_sleep_rhythm(state, state.current_action, interrupted=True)
        elif config.mode == "autonomous" and state.sleep_rhythm.get("decision") == "sleep":
            # 临睡时还没有实际睡眠，取消待睡决定，但保留已有清醒负担。
            state.sleep_rhythm.pop("retry_at", None)
            state.sleep_rhythm.update(decision="continue", source="wake",
                reason="临睡时被叫回清醒，稍后重新评估",
                next_evaluation_at=self._now() + max(config.evaluation_minutes, config.wake_grace_minutes) * 60,
                planned_wake_at=0)
        state.current_action = None
        state.state = STATE_IDLE
        state.mood_override_until = 0
        # 醒着以后不许再挂着"正要睡"：临睡期的记账、揣着的那一步一起清掉
        state.drowsy_sleep_step = {}
        state.drowsy_started_world_time = 0
        state.drowsy_started_at = 0.0
        if config.clear_plan_on_wake:
            state.current_plan = None
        grace_minutes = max(0, int(config.wake_grace_minutes))
        if grace_minutes:
            grace_ticks = max(1, int(round(grace_minutes * 60 / self.tick_seconds)))
            state.no_sleep_until = max(
                int(state.no_sleep_until), int(state.world_time) + grace_ticks
            )
        # 群名片要立刻从「睡觉中」改回来，所以顺手把冷却清零
        state.last_nickname_update_at = 0.0
        state.add_event("wake_up", {"by": ctx.user_id})
        # 「刚被叫醒」这句提示：接管这一轮会用到，写在状态里统一取
        state.wake_note = self._wake_note_text(ctx)
        state.wake_note_until = int(state.world_time) + 2

    async def should_block_sleep(self, ctx: MessageContext) -> bool:
        """睡着时要不要把这条消息整个挡下来（挡在其它插件之前）。

        默认只挡「没 @ 她」的消息：她被 @ 了、消息是 ``/指令``、或者本来就是私聊，
        都放行给后面的正常流程（@ 她会拿到固定文案，或者命中唤醒词被叫醒）。
        ``sleep.block_scope = all`` 时连「@ 了她但没说唤醒词」的消息也一起挡。
        """

        config = self.world.sleep
        if not config.block_plugins or not self.is_enabled(ctx.session_id):
            return False
        if ctx.is_private:
            return False  # 私聊等同被叫，永远放行
        if self._wake_hit(ctx):
            return False  # 明确叫醒 -> 放行
        if ctx.is_mentioned and config.block_scope != "all":
            return False  # @ 了她但没说唤醒词 -> 交给 sleep_gate 回固定文案
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return False
            # 挡下来之前先记一笔：她醒来还得知道群里发生过什么
            self._note_presence(state, ctx)
            # 也是"吵"的一笔：吵到位就把这条放行，让她迷糊回一句（她还算睡着）
            self._note_sleep_noise(state, ctx)
            if await self._maybe_startle_wake(state, ctx):
                return False
            # 睡着的时候群里刷屏，不能每来一条就写一条日志——日志页会被刷爆。
            # 每 10 分钟最多记一条，并带上这段时间一共挡下了多少条。
            now = self._now()
            state.sleep_skip_count = int(state.sleep_skip_count) + 1
            if state.sleep_skip_at and now - float(state.sleep_skip_at) < 600.0:
                return True
            suppressed = int(state.sleep_skip_count)
            state.sleep_skip_count = 0
            state.sleep_skip_at = now
            await self._log_event(
                state,
                "sleep_skip",
                {
                    "user": ctx.user_name or ctx.user_id or "有人",
                    "reason": "她在睡觉，这条消息被整个挡下（其它插件也不会执行）",
                    "count": suppressed,
                },
            )
            return True

    async def sleep_gate(self, ctx: MessageContext) -> SleepReply | None:
        """她在睡觉时挡在回复路径前面的那道门。

        返回 ``None`` 表示照常回复（没在睡，或者这条消息把她叫醒了）；
        否则调用方按返回的 ``mode`` 处理，并且**不要**再交给主人格。
        """

        config = self.world.sleep
        if config.reply_mode == "normal" or not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None
        async with self.session_state(ctx.session_id) as state:
            if not self._is_asleep(state):
                return None
            if self._wake_hit(ctx):
                return None  # 正常路径会负责叫醒她
            # @ 她 / 私聊也算"吵"：连着来几次就把她吵醒一次，之后三分钟里她能真回话
            self._note_sleep_noise(state, ctx)
            if await self._maybe_startle_wake(state, ctx):
                return None
            if self._startled_active(state):
                return None  # 迷糊窗口里：让她自己回，而不是那句固定文案
            who = ctx.user_name or ctx.user_id or "有人"
            echo = TickOutcome(session_id=ctx.session_id)
            echo.place = ctx.session_id
            if not (ctx.is_mentioned or ctx.is_private):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "她在睡觉，而且这条消息没 @ 她"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="没在跟她说话")
            if config.reply_mode == "silent":
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "配置成睡觉时保持安静"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="睡觉时保持安静")
            text = self._sleep_reply_text(state, ctx)
            if not text:
                return SleepReply(mode="silent", reason="固定文案是空的")
            if self._sleep_reply_on_cooldown(state):
                await self._log_event(
                    state,
                    "sleep_skip",
                    {"user": who, "reason": "刚回睡觉文案不久，先安静着"},
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)
                return SleepReply(mode="silent", reason="固定文案还在冷却")
            state.sleep_reply_at = self._now()
            # 睡觉时回的这句也是"被搭话后的回复"，同样要压一压后面的主动搭话
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            await self._log_event(
                state, "sleep_reply", {"user": who, "text": text}, outcome=echo
            )
            self._queue_echo(ctx.session_id, echo.debug_messages)
            return SleepReply(mode="template", messages=[text], reason="她在睡觉")

    @session_scoped
    async def handle_incoming(self, ctx: MessageContext) -> str | None:
        """处理一条用户消息：非空 = **这条归插件管**，None = 不归我管。

        - 不在白名单 / 内容安全拦截 -> 返回 None（本插件完全不干预，交给宿主；
          接管失败也不会回落成"主人格替她答"）；
        - 计数、状态、日志、"待总结"缓冲这些副作用都在这里完成；
        - 提示词不在这里拼：接管路径自己组装（见 :meth:`handle_reply`）。
        """

        if not self.is_enabled(ctx.session_id):
            return None
        if not self._passes_safety(ctx.text):
            return None

        async with self.session_state(ctx.session_id) as state:
            node = self.node(state.node_id)
            if node is None:
                state.node_id = self.default_node_id()
                node = self.node(state.node_id)

            extra_notes, woke = self._record_user_message(state, ctx)
            if extra_notes:
                # 「刚被叫醒 / 冷启动」这类临时提示要等真正组装提示词时才写得进去
                # （接管路径在 handle_reply 里组装）：先按会话存着，取走即失效。
                self._incoming_notes[ctx.session_id] = list(extra_notes)
            # 刚被搭话（她马上要回一句）：接下来一段时间别再因为孤独感主动开口
            self.engagement.note_passive_reply(state, tick_seconds=self.tick_seconds)
            if woke:
                echo = TickOutcome(session_id=ctx.session_id)
                echo.place = ctx.session_id
                await self._log_event(
                    state,
                    "wake_up",
                    {"by": ctx.user_name or ctx.user_id},
                    persona_id=ctx.persona_id,
                    outcome=echo,
                )
                self._queue_echo(ctx.session_id, echo.debug_messages)

            # 把这次对话放进"待总结"缓冲：一段聊完或她离开这个地点时，
            # 才让模型把它压成一条记忆（逐条存「谁说了什么」没有信息量）。
            self.note_dialogue(
                state,
                user_id=ctx.user_id,
                name=ctx.user_name or ctx.user_id,
                text=ctx.text,
                persona_id=ctx.persona_id,
                source_id=ctx.event_key,
                origin=ctx.session_id,
            )
            await self._log_event(
                state,
                "user_message",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    "len": len(ctx.text or ""),
                    "text": (ctx.text or "")[:120],
                },
                persona_id=ctx.persona_id,
                place=ctx.session_id,
            )
            return "takeover"
