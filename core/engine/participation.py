"""Message-driven, bounded opportunities to join a conversation without taking over chat."""
from __future__ import annotations

import asyncio
import time
import random

from ..config_scope import session_scoped
from ..model_calls import model_operation
from ..participation import build_participation_prompt, parse_participation
from ..state import STATE_DROWSY
from .life_inference import life_work
from .models import TickOutcome


class ParticipationMixin:
    def claim_chat_scene(self, outcome, target: str, rows: list[dict]) -> None:
        outcome.chat_claims[target] = [str(row["seq"]) for row in rows]
        outcome.chat_expires_at[target] = max(float(row.get("at") or 0) for row in rows) + self.world.decider.live_window_seconds

    def conversation_scene_hint(self, state, target: str) -> str:
        rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                and self._now() - float(row.get("at") or 0) <= self.world.decider.live_window_seconds][-8:]
        lines, _ = self.prompts.render_chat_rows(rows, 180, now=self.local_now(), current_session=target)
        return ("\n# 准备说话的地方此刻在聊什么\n" + self.session_label(target, state) + "\n"
                + ("\n".join(lines) or "最近没有新的对话；不要假定有人正等你汇报。")
                + "\n这些只是聊天背景，不是新的任务或对你的要求。"
                "先看回应对象与当前话题，别把生活经历写成广播；没人正在关心这件事时可以不说。\n")

    def begin_native_turn(self, turn: dict) -> None:
        turns = getattr(self, "_participation_native_turns", {})
        turns = {key: item for key, item in turns.items() if self._now() - item.get("started_at", 0) < 300}
        turns[id(turn)] = turn
        self._participation_native_turns = turns

    def end_native_turn(self, turn: dict) -> None:
        getattr(self, "_participation_native_turns", {}).pop(id(turn), None)

    def native_turn_busy(self, session_id: str) -> bool:
        return any(not turn.get("response_error")
                   and self._now() - turn.get("started_at", 0) < 300
                   and self.state_key(str(turn.get("session_id") or "")) == self.state_key(session_id)
                   for turn in getattr(self, "_participation_native_turns", {}).values())

    def live_chat(self, state, session_id: str = "", *, fresh: bool = True) -> list[dict]:
        target = str(session_id or state.session_id)
        cutoff = self._now() - self.world.decider.live_window_seconds
        rows = self.chat_context(state, target) if fresh else state.recent_chat
        return [row for row in rows
                if self._chat_origin(row, state) == target and not row.get("is_self")
                and float(row.get("at") or 0) >= cutoff
                and not self.is_plugin_command(str(row.get("text") or ""))]

    def participation_gate(self, state, target: str) -> str:
        if not self.is_enabled(target) or not self.proactive_chat_allowed(target):
            return "paused"
        if not self.world.decider.enabled:
            return "disabled"
        if not self.world.decider.message_driven:
            return "message_driven_disabled"
        if state.is_sleeping or state.state == STATE_DROWSY or state.energy < .25:
            return "resting"
        if state.current_action:
            definition = self.world.action_map().get(str(state.current_action.get("type") or ""))
            if (not definition or not definition.interruptible or definition.llm_level != "template"
                    or self._action_speaks(definition) or definition.target_type not in {"none", "node"}):
                return "busy"
        if state.current_plan and (not state.current_action
                or state.current_plan.get("source") in {"reply", "schedule"}):
            return "busy"
        if any(not item.get("absorbed") and not item.get("picked")
               for item in self._pending_replies.get(self.merge_scope(target), [])):
            return "reply_pending"
        if self.native_turn_busy(target):
            return "reply_pending"
        return ""

    def participation_prompt(self, state, target, persona, *, rows=None, request=None):
        """Same builder for production and evaluation; never include another session."""
        if rows is None:
            rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                    and self._now() - float(row.get("at") or 0) <= 600
                    and not self.is_plugin_command(str(row.get("text") or ""))]
        from ..context_selection import CONTEXT_GUIDANCE, ContextItem, select_context
        from ..context_sources import life_items
        request = request or self.context_request(state, target, task="participation", rows=rows)
        from hashlib import sha256
        request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
        rows = self.context_chat(state, request, rows)
        node = self.node(state.node_id) or self.node(self.default_node_id())
        _, _, style = self.style_for(state, target)
        action = state.current_action or {}
        activity = (str(action.get("desc") or action.get("intent") or self.prompts.action_label(action.get("type")))
                    if action else "自由活动")
        life = {"place": node.name if node else "", "mood": state.mood,
                "local_time": self.local_now().isoformat(timespec="minutes"),
                "activity": activity}
        materials = life_items(state, request) if self.world.state_dynamics.own_topic_enabled else []
        for memory in self.context_memories(state, request, rows):
            label = "本会话阅读时的主观感想，非亲历：" if memory.source == "chat_browse" else "相关经历："
            if memory.private_matter:
                label = "本会话约定的生活进展（准备与模拟不等于现实兑现）："
            materials.append(ContextItem(f"memory:{memory.id}", "memory",
                label + ("涉及用户ID：" + "、".join(memory.participants or memory.related_users) + "\n" if memory.participants or memory.related_users else "")
                + memory.render(stamp=self.prompts._memory_stamp(memory.created_at, self.local_now())),
                at=memory.created_at, people=tuple(memory.participants or memory.related_users),
                session=memory.session_id if memory.source_scoped else ""))
        background = select_context(request, materials).render()
        profile = self.context_profiles(state, request)
        background = "\n\n".join(part for part in (CONTEXT_GUIDANCE, profile, self.context_emotions(state, request), background) if part)
        result = build_participation_prompt(persona=persona, life=life, rows=rows,
            now=self._now(), live_window=self.world.decider.live_window_seconds, style=style,
            background=background)
        request.trace["scene"] = {"rows": len(rows), "characters": len(result[1])}
        return result

    def participation_sample(self, state, target, new) -> bool:
        """Spectre-style opportunity sampling per burst, softened by recent speech."""
        p = {"quiet": .08, "natural": .18, "lively": .35}[self.world.decider.participation_level]
        recent = [r for r in state.recent_chat if self._chat_origin(r, state) == target
                  and self._now() - float(r.get("at") or 0) <= 120]
        own = sum(bool(r.get("is_self")) for r in recent)
        p /= 1 + own
        # Explicit continuation gets a full read; messages addressed to other
        # people still go through the ordinary sampling/read-air decision.
        if any(r.get("addressing") == "me" for r in new):
            return True
        return random.random() < 1 - (1 - p) ** min(len(new), 8)

    async def participation_text(self, state, target) -> tuple[str, str]:
        if self.llm is None:
            return "", ""
        rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                and self._now() - float(row.get("at") or 0) <= 600
                and not self.is_plugin_command(str(row.get("text") or ""))]
        request = self.context_request(state, target, task="participation", rows=rows)
        await self.prepare_context_memory(request)
        system, prompt, targets = self.participation_prompt(state, target,
            await self._persona_text(target), request=request, rows=rows)
        self.record_context_selection(request)
        raw = await self._ask_llm(target, system, prompt)
        return parse_participation(raw or "", targets)

    def queue_participation(self, session_id: str) -> None:
        """One settling job per life; new messages update its target without spawning workers."""
        if not self.is_enabled(session_id) or not self.chat_is_group(session_id):
            return
        with self.config_scope(session_id):
            if not self.world.decider.message_driven or not self.proactive_chat_allowed(session_id):
                return
        key = self.state_key(session_id)
        if getattr(self, "_background_closed", False):
            return
        pending = getattr(self, "_participation_pending", None)
        if pending is None:
            pending = self._participation_pending = {}
        pending[key] = (session_id, time.monotonic())
        settlers = getattr(self, "_participation_settlers", None)
        if settlers is None:
            settlers = self._participation_settlers = {}
        if key in settlers and not settlers[key].done():
            return

        async def settle():
            started = time.monotonic()
            while True:
                target, arrived = pending[key]
                with self.config_scope(target):
                    delay = min(self.world.decider.settle_seconds - (time.monotonic() - arrived),
                                12 - (time.monotonic() - started))
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                active = getattr(self, "_background_jobs", {}).get("participation:" + key)
                if active and not active.done():
                    await asyncio.shield(active)
                    continue
                # The last burst must not disappear merely because it arrived
                # during inference or a cooldown. Wait outside the worker pool,
                # then recheck both the scene's freshness and the character's life.
                with self.config_scope(target):
                    async with self.session_state(target) as state:
                        observation = state.participation.get(target) or {}
                        rows = self.live_chat(state, target)
                        if (self.participation_gate(state, target)
                                or not any(int(row.get("seq") or 0) > observation.get("checked_seq", 0)
                                           for row in rows)):
                            return
                considered = pending[key]
                task = self.queue_background("participation:" + key, lambda: self.maybe_participate(target, sample=True))
                if task is None:
                    return
                await asyncio.shield(task)
                if pending[key] == considered:
                    return
                started = time.monotonic()

        # Waiting for a person to finish typing must not occupy a model worker.
        task = asyncio.create_task(settle(), name="my_world:listen:" + key)
        settlers[key] = task
        def finished(done):
            if settlers.get(key) is done:
                settlers.pop(key, None)
                pending.pop(key, None)
            if not done.cancelled() and done.exception():
                self._log("debug", "群聊聚合留待下一条消息")
        task.add_done_callback(finished)

    @session_scoped
    @life_work()
    @model_operation("participation", "听完近期群聊，决定是否自然接一句")
    async def maybe_participate(self, session_id: str, *, sample: bool = False) -> TickOutcome | None:
        if not self.is_enabled(session_id) or not self.chat_is_group(session_id):
            return None
        outcome = TickOutcome(session_id=session_id, speech_kind="participation", place=session_id)
        async with self.session_state(session_id) as state:
            gate = self.participation_gate(state, session_id)
            rows = self.live_chat(state, session_id)
            observation = dict(state.participation.get(session_id) or {})
            new = [r for r in rows if int(r.get("seq") or 0) > observation.get("checked_seq", 0)]
            if gate or not new:
                return None
            # Listening is a completed decision too. Persist before inference so a
            # discarded/empty response cannot re-evaluate the same burst forever.
            state.participation[session_id] = {"checked_seq": max(int(r.get("seq") or 0) for r in rows),
                                                "checked_at": self._now(), "decision": "listening"}
            if sample and not self.participation_sample(state, session_id, new):
                return outcome
            if self.llm is None:
                return outcome
            self.decision_trace(session_id, "participation", "considering", "近期群聊已聚合，判断是否有话想接",
                                messages=len(rows))
            self.claim_chat_scene(outcome, session_id, rows)
            message, reply_to = await self.participation_text(state, session_id)
            if (not message or self.participation_gate(state, session_id)
                    or self._now() >= outcome.chat_expires_at[session_id]):
                self.decision_trace(session_id, "participation", "listening", "这轮继续旁听")
                return outcome
            outcome.add_speech(message)
            if reply_to:
                outcome.reply_targets[session_id] = reply_to
        await self._deliver(outcome)
        return outcome

    @staticmethod
    def _contact_person_key(target: str, user_id: str) -> str:
        return target.split(":", 1)[0] + ":" + str(user_id)

    def _outreach_ledger(self, session_id):
        ledger = self.db.raw.kv_get("outreach:" + self.state_key(session_id), {}) or {}
        people = {}
        for uid, record in (ledger.get("people") or {}).items():
            # v4.29.0 used raw user IDs; their recorded origin supplies the platform.
            uid = str(record.get("user_id", uid))
            record = {**record, "user_id": uid}
            key = self._contact_person_key(str(record.get("origin") or session_id), uid)
            if key not in people or record.get("at", 0) > people[key].get("at", 0):
                people[key] = record
        ledger["people"] = people
        return ledger

    def outreach_gate(self, target: str, *, token: str = "", user_id: str = "") -> str:
        """All autonomous entrances share actual-send cadence and unanswered-person backoff."""
        with self.config_scope(target):
            if self.native_turn_busy(target):
                return "正在回应别人，先听一会儿"
            ledger = self._outreach_ledger(target)
            last = (ledger.get("sessions") or {}).get(target) or {}
            same_turn = bool(token and last.get("token") == token
                             and self._now() - last.get("first_at", last.get("at", 0)) <= 120)
            if last and not same_turn and self._now() - last.get("at", 0) < self.world.decider.min_speech_gap_seconds:
                return "刚说过话，先听一会儿"
            waiting = ledger["people"].get(self._contact_person_key(target, user_id)) or {}
            same_person_turn = same_turn and waiting.get("token") == token
            if user_id and waiting and not same_person_turn and self._now() - waiting.get("at", 0) < self.world.decider.person_retry_hours * 3600:
                return "上次找他还没有回应，先给他一点时间"
        return ""

    def note_visible_speech(self, target: str, *, token: str = "", people=()) -> None:
        key = "outreach:" + self.state_key(target)
        ledger = self._outreach_ledger(target)
        now = self._now()
        sessions = ledger.setdefault("sessions", {})
        previous = sessions.get(target) or {}
        first = previous.get("first_at", previous.get("at", now)) if token and previous.get("token") == token else now
        sessions[target] = {"at": now, "first_at": first, "token": token}
        waiting = ledger.setdefault("people", {})
        for uid in people:
            waiting[self._contact_person_key(target, uid)] = {
                "at": now, "token": token, "origin": target, "user_id": str(uid)}
        # Bounded even in installations with many contacts and changing sessions.
        for field, maximum in (("sessions", 128), ("people", 256)):
            ledger[field] = dict(sorted(ledger[field].items(), key=lambda pair: pair[1].get("at", 0))[-maximum:])
        self.db.raw.kv_set(key, ledger)

    def note_contact_response(self, state, ctx) -> None:
        if self.reply_addressing(state, ctx) == "interject":
            return
        key = "outreach:" + self.state_key(ctx.session_id)
        ledger = self._outreach_ledger(ctx.session_id)
        if ledger["people"].pop(self._contact_person_key(ctx.session_id, ctx.user_id), None) is not None:
            self.db.raw.kv_set(key, ledger)
        # An unrelated chat in another session cannot acknowledge this outreach.
        origins = set(getattr(state, "engagement_origins", []) or [])
        if not origins or ctx.session_id in origins:
            self.engagement.on_user_replied(state)
            state.engagement_origins = []
