"""Message-driven, bounded opportunities to join a conversation without taking over chat."""
from __future__ import annotations

import asyncio
import time

from ..config_scope import session_scoped
from ..model_calls import model_operation
from ..participation import build_participation_prompt
from ..state import STATE_DROWSY
from .life_inference import life_work
from .models import TickOutcome


class ParticipationMixin:
    def _peer_chat_policy(self, target):
        with self.config_scope(target):
            config = self.world.decider
            platform = target.split(":", 1)[0]
            peers = {value.strip().removeprefix(platform + ":") for value in config.peer_bot_ids
                     if value.strip() and (":" not in value or value.startswith(platform + ":"))}
            key = "peer_chat:" + self.state_key(target)
            if not peers:
                return key, {}, peers, config
            ledger = self.db.raw.kv_get(key, {}) or {}
            policy = [self.world.world_id, sorted(peers)]
            if ledger.get("policy") != policy:
                ledger = {"policy": policy, "sessions": {}}
            return key, ledger, peers, config

    def note_peer_chat_input(self, target, row):
        from ..chat_cadence import note_input
        key, ledger, peers, _ = self._peer_chat_policy(target)
        if not peers:
            return
        sessions = dict(ledger.get("sessions", {}))
        sessions[target] = note_input(sessions.get(target, {}), row, peers)
        self.db.raw.kv_set(key, {**ledger, "sessions": sessions})

    def note_peer_chat_receipt(self, outcome, target, kind):
        from .proactive_contact import CHAT_KINDS
        if kind not in CHAT_KINDS:
            return
        from ..chat_cadence import note_receipt
        key, ledger, peers, config = self._peer_chat_policy(target)
        if not peers:
            return
        sessions = dict(ledger.get("sessions", {}))
        sessions[target] = note_receipt(sessions.get(target, {}), outcome.speech_receipt_batch,
            self._now(), config.continuation_window_seconds,
            retention=max(config.continuation_window_seconds, config.peer_chat_pause_seconds))
        self.db.raw.kv_set(key, {**ledger, "sessions": sessions})

    def peer_chat_pause(self, target, *, token=""):
        from ..chat_cadence import pause_until
        _, ledger, peers, config = self._peer_chat_policy(target)
        return (pause_until(ledger.get("sessions", {}).get(target, {}), now=self._now(),
            cap=config.peer_chat_max_turns, pause=config.peer_chat_pause_seconds,
            window=config.continuation_window_seconds, token=token) if peers else 0.)

    # Arrival registration predicts host dispatch, but cannot prove a reply is
    # running. Keep a five-minute bound only for unowned arrival registrations.
    REPLY_EXPECTATION_SECONDS = 300

    def pending_reply_busy(self, session_id: str) -> bool:
        scope = self.merge_scope(session_id)
        if any(not task.done() for task in getattr(self, '_active_reply_tasks', {}).get(scope, ())):
            return True
        now = time.monotonic()
        owners = getattr(self, '_pending_reply_owners', {}).get(scope, {})
        return any(not item.get('absorbed') and not item.get('picked')
                   and self._reply_event_open(scope, item)
                   and (id(item) in owners or
                        0 <= now - float(item.get('at') or 0) < self.REPLY_EXPECTATION_SECONDS)
                   for item in self._pending_replies.get(scope, []))

    def reply_busy(self, session_id: str) -> bool:
        """One conversation priority check from arrival through native sending."""
        return self.pending_reply_busy(session_id) or self.native_turn_busy(session_id)

    def _participation_window(self, source: str = "message") -> int:
        # Reading itself lasts 3–8 minutes. A three-minute live window would
        # expire every message she picked up before she could finish reading.
        return 900 if source in {"browse", "completion"} else self.world.decider.live_window_seconds

    def claim_chat_scene(self, outcome, target: str, rows: list[dict], *, window: int | None = None,
                         state=None, media_rows=None) -> None:
        outcome.chat_claims[target] = [str(row["seq"]) for row in rows]
        outcome.chat_expires_at[target] = max(float(row.get("at") or 0) for row in rows) + (
            self.world.decider.live_window_seconds if window is None else window)
        from copy import deepcopy
        history = [r for r in state.recent_chat if self._chat_origin(r, state) == target] if state else rows
        outcome.chat_media_scenes[target] = deepcopy({"rows": history,
            "new": rows if media_rows is None else media_rows})

    def queued_chat_media_allowed(self, outcome, target: str) -> bool:
        started = outcome.accepted_speech_at.get(target)
        if started is not None and 0 <= self._now() - started <= 120:
            return True
        scene = outcome.chat_media_scenes.get(target)
        if scene is None:
            return True  # Legacy/custom outcomes have no captured media evidence.
        from ..participation_opportunity import media_scene_allowed
        with self.config_scope(target):
            return media_scene_allowed(**scene, now=self._now(), mode=self.world.decider.media_only_participation)

    def chat_media_allowed(self, state, target: str, rows: list[dict]) -> bool:
        """Use the actual destination's current policy before and after inference."""
        from ..participation_opportunity import media_scene_allowed
        with self.config_scope(target):
            return media_scene_allowed(rows=[r for r in state.recent_chat
                if self._chat_origin(r, state) == target], new=rows, now=self._now(),
                mode=self.world.decider.media_only_participation)

    def conversation_scene_hint(self, state, target: str) -> str:
        rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                and self._now() - float(row.get("at") or 0) <= self.world.decider.live_window_seconds][-8:]
        lines, _ = self.prompts.render_chat_rows(rows, 180, now=self.local_now(), current_session=target)
        return ("\n# 准备说话的地方此刻在聊什么\n" + self.session_label(target, state) + "\n"
                + ("\n".join(lines) or "最近没有新的对话；不要假定有人正等你汇报。")
                + "\n这些只是聊天背景，不是新的任务或对你的要求。"
                "先看回应对象与当前话题，别把生活经历写成广播；没人正在关心这件事时可以不说。\n")

    def _native_turn_open(self, turn: dict) -> bool:
        owner = getattr(self, '_participation_native_owners', {}).get(id(turn))
        if owner is not None:
            return not owner.done()
        return 0 <= self._now() - turn.get('started_at', 0) < self.REPLY_EXPECTATION_SECONDS

    def begin_native_turn(self, turn: dict, *, owner: asyncio.Task | None = None) -> None:
        """Track the real host pipeline; time alone cannot expire a running reply."""
        turns = getattr(self, "_participation_native_turns", {})
        turns = {key: item for key, item in turns.items() if self._native_turn_open(item)}
        owners = {key: task for key, task in getattr(self, '_participation_native_owners', {}).items()
                  if key in turns}
        key = id(turn)
        turns[key] = turn
        self._participation_native_turns = turns
        self._participation_native_owners = owners
        if owner is not None:
            owners[key] = owner

            def finished(completed: asyncio.Task) -> None:
                if self._participation_native_owners.get(key) is completed:
                    self._participation_native_owners.pop(key, None)
                    self._participation_native_turns.pop(key, None)

            owner.add_done_callback(finished)

    def end_native_turn(self, turn: dict) -> None:
        getattr(self, "_participation_native_turns", {}).pop(id(turn), None)
        getattr(self, '_participation_native_owners', {}).pop(id(turn), None)

    def native_turn_busy(self, session_id: str) -> bool:
        return any(not turn.get("response_error")
                   and self._native_turn_open(turn)
                   and self.state_key(str(turn.get("session_id") or "")) == self.state_key(session_id)
                   for turn in getattr(self, "_participation_native_turns", {}).values())

    def live_chat(self, state, session_id: str = "", *, fresh: bool = True, window: int | None = None) -> list[dict]:
        target = str(session_id or state.session_id)
        cutoff = self._now() - (self.world.decider.live_window_seconds if window is None else window)
        rows = self.chat_context(state, target) if fresh else state.recent_chat
        return [row for row in rows
                if self._chat_origin(row, state) == target and not row.get("is_self")
                and float(row.get("at") or 0) >= cutoff
                and not self.is_plugin_command(str(row.get("text") or ""))]

    def participation_gate(self, state, target: str, *, source: str = "message") -> str:
        if not self.is_enabled(target) or not self.proactive_chat_allowed(target):
            return "paused"
        if source == "browse" and not self.speech_allowed(target, "browse_reply"):
            return "disabled"
        if source != "browse" and not self.world.decider.enabled:
            return "disabled"
        if source != "browse" and not self.world.decider.message_driven:
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
        if self.reply_busy(target):
            return "reply_pending"
        if self.peer_chat_pause(target):
            return "peer_cooldown"
        return ""

    def participation_prompt(self, state, target, persona, *, rows=None, request=None, source="message", browse_id=""):
        """Same builder for production and evaluation; never include another session."""
        if rows is None:
            rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                    and self._now() - float(row.get("at") or 0) <= max(600, self._participation_window(source))
                    and not self.is_plugin_command(str(row.get("text") or ""))]
        from ..context_selection import CONTEXT_GUIDANCE, relevance, select_context
        from ..context_sources import browse_feeling_item, life_items, memory_context_items
        request = request or self.context_request(state, target, task="participation", rows=rows)
        from hashlib import sha256
        request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
        rows = self.context_chat(state, request, rows)
        _, _, style = self.style_for(state, target, remember=False)
        life = {"mood": state.mood,
                "local_time": self.local_now().isoformat(timespec="minutes")}
        materials = []
        # Speech uses life evidence only when this conversation actually touches it.
        # Keep intelligent selection for questions/continuations without offering an
        # unrelated unfinished project as a new topic every time she joins a chat.
        if self.world.state_dynamics.own_topic_enabled:
            materials = [item for item in life_items(state, request)
                         if relevance(request.query, item.topic_text)]
            import re
            if re.search(r"你.{0,6}(?:在(?:哪|做|干)|忙什么|刚才|最近怎么样|今天怎么样|做完)", request.query):
                node = self.node(state.node_id)
                action = state.current_action or {}
                life.update(place=node.name if node else "", activity=str(action.get("desc") or action.get("intent") or "自由活动"))
        memories = self.context_memories(state, request, rows)
        current_feeling = browse_feeling_item(state, request, browse_id) if source == "browse" else None
        if current_feeling:
            from ..context_sources import life_memory_metadata
            memories = [item for item in memories
                        if not (item.source == "chat_browse"
                                and life_memory_metadata(item.source, item.context).get("browse_id") == browse_id)]
        materials += memory_context_items(state, request, memories,
            stamp=lambda at: self.prompts._memory_stamp(at, self.local_now()),
            life_enabled=self.world.state_dynamics.own_topic_enabled)
        if current_feeling:
            # Highest ordinary priority; still inside the shared context budget.
            # Its key replaces the generic thought instead of repeating it.
            materials.append(current_feeling)
        background = select_context(request, materials).render()
        profile = self.context_profiles(state, request)
        background = "\n\n".join(part for part in (CONTEXT_GUIDANCE, profile, self.context_emotions(state, request), background) if part)
        if source == "browse":
            background += ("\n\n# 刚翻过消息\n你刚安静看过这段对话。看清每条消息距今多久，以及后续是否换了话题、"
                           "问题是否已解决。回看范围只代表还可以考虑，不代表现在应该回复。"
                           "接较早的话时选择引用，话题已结束或问题已解决时不追答。")
        configured_silence = bool(self.world.decider.natural_silence)
        # An autonomous opportunity invites a thought, never orders speech.
        # The host's direct-reply path is unaffected.
        natural_silence = True
        from ..participation_opportunity import visible_turns
        turn_count = len(visible_turns([row for row in state.recent_chat
            if self._chat_origin(row, state) == target], self._now(),
            window=self.world.decider.continuation_window_seconds))
        background += (f"\n# 这段对话的节奏\n统计窗口内你已发言 {turn_count} 个完整回合。"
                       "被引用或收到另一句回应不等于必须再说一句；先看是否有新的问题、信息、情绪变化或想交流的细节。"
                       "重复争辩同一个点、只剩附和或互相逗一句时，可以自然收尾，安静回到自己的生活。"
                       "有值得回应的新内容时仍可继续，不输出宣布沉默、等待或结束的占位台词。")
        result = build_participation_prompt(persona=persona, life=life, rows=rows,
            now=self._now(), live_window=self._participation_window(source), style=style,
            background=background, natural_silence=natural_silence)
        request.trace["scene"] = {"rows": len(rows), "characters": len(result[1])}
        request.trace["natural_silence"] = natural_silence
        request.trace["silence_policy"] = {
            "source": source, "configured": configured_silence, "effective": natural_silence,
        }
        return result

    def participation_sample(self, state, target, new, persona: str = "") -> bool:
        """Compatibility name: deterministic attention, not a random speech gate."""
        from ..participation_opportunity import assess_opportunity
        observation = state.participation.setdefault(target, {})
        attempts = [at for item in state.participation.values() for at in item.get("attempts", [])]
        affinities = {}
        if self.profiles.enabled():
            for uid in dict.fromkeys(str(row.get("user_id") or "") for row in new):
                if uid:
                    person = self.profiles.profile(target, uid)
                    if person:
                        high = max(1., float(self.world.profile.affinity_max))
                        affinities[uid] = float(person.get("affinity") or 0) / high
        rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target]
        choice = assess_opportunity(now=self._now(), rows=rows, new=new, observation=observation,
            attempts=attempts, persona=persona, affinities=affinities,
            level=self.world.decider.participation_level,
            hourly_max=self.world.decider.participation_hourly_max,
            relation_weight=self.world.decider.participation_relation_weight,
            continuation_interval=self.world.decider.continuation_interval_seconds,
            continuation_turns=self.world.decider.continuation_priority_turns,
            continuation_window=self.world.decider.continuation_window_seconds,
            media_mode=self.world.decider.media_only_participation)
        observation["opportunity"] = choice.payload()
        return choice.ready

    @staticmethod
    def finish_participation_observation(state, target: str, request_id: str, status: str, reason: str) -> None:
        """A late cancellation or receipt must never finish a newer observation."""
        observation = state.participation.get(target) or {}
        if observation.get("request_id") == request_id and observation.get("decision") == "considering":
            observation.update(decision=status, reason=reason)

    @staticmethod
    def note_participation_waiting(state, target: str, gate: str) -> None:
        reason = {
            "reply_pending": "正在回应对话，结束后只看最新现场",
            "busy": "先完成手头活动，再看仍在进行的现场",
            "resting": "正在休息，暂未判断是否接话",
            "peer_cooldown": "刚与其他机器人连续聊过，先留白；有人带来新发言时再看",
        }.get(gate)
        if reason:
            state.participation.setdefault(target, {}).update(decision="waiting", reason=reason)

    async def participation_text(self, state, target, *, source="message", browse_id="", outcome=None) -> tuple[str, str]:
        if self.llm is None:
            return "", ""
        rows = [row for row in state.recent_chat if self._chat_origin(row, state) == target
                and self._now() - float(row.get("at") or 0) <= max(600, self._participation_window(source))
                and not self.is_plugin_command(str(row.get("text") or ""))]
        request = self.context_request(state, target, task="participation", rows=rows)
        await self.prepare_context_memory(request)
        self.style_for(state, target)
        persona = await self._persona_text(target)
        if source == "browse":
            from hashlib import sha256
            from ..context_sources import browse_feeling_item
            request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
            if browse_feeling_item(state, request, browse_id) is None:
                return "", ""
        system, prompt, targets = self.participation_prompt(state, target,
            persona, request=request, rows=rows, source=source, browse_id=browse_id)
        self.record_context_selection(request)
        raw = await self.ask_speech(state, target, system, prompt, rows=rows, live_window=self._participation_window(source))
        original_draft = raw
        from ..speech_decision import parse_decision
        from ..json_actions import extract_json_object
        import json
        feeling = next((item for item in reversed(state.thoughts)
                        if browse_id and item.get("browse_id") == browse_id), None)
        if feeling and isinstance(extract_json_object(raw or ""), dict):
            payload = extract_json_object(raw)
            payload["inner"] = feeling["content"][:240]
            raw = json.dumps(payload, ensure_ascii=False)
        decision = parse_decision(raw or "", targets, allowed=("join_chat", "continue_chat", "listen"))
        if decision.error and raw and not feeling:
            # One bounded repair under the same background budget. Never invent
            # thoughts from the draft and never replay a failed provider call.
            raw = await self.ask_speech(state, target, system, prompt + "\n上轮结果未通过校验："
                + decision.error + "。请重新给出完整 JSON，补上真实想法，并保留本轮发言选择。"
                + "\n上轮草稿（只修复格式，不添加新事实）：" + str(raw)[:5000], rows=rows, allow_tools=False)
            decision = parse_decision(raw or "", targets, allowed=("join_chat", "continue_chat", "listen"))
        if decision.error:
            self.decision_trace(target, "participation", "failed", decision.error)
            state.participation[target] = {**state.participation.get(target, {}),
                                           "decision": "failed", "reason": decision.error}
            await self._log_event(state, "speech_decision_failed", {"trigger": source, "reason": decision.error}, outcome=outcome, silent=True)
            return "", ""
        await self.remember_speech_decision(state, target, decision, trigger=source,
                                          outcome=outcome, browse_id=browse_id, raw=original_draft)
        return decision.say, decision.reply_to

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
        pending.setdefault(key, {})[session_id] = time.monotonic()
        settlers = getattr(self, "_participation_settlers", None)
        if settlers is None:
            settlers = self._participation_settlers = {}
        if key in settlers and not settlers[key].done():
            return

        async def settle():
            started = time.monotonic()
            retry_at = {}
            while pending.get(key):
                # One life has one worker, but every actual session keeps its
                # own burst. A hot group cannot overwrite a quieter one.
                target = min(pending[key], key=lambda sid: (retry_at.get(sid, 0), pending[key][sid]))
                arrived = pending[key][target]
                with self.config_scope(target):
                    delay = min(self.world.decider.settle_seconds - (time.monotonic() - arrived),
                                12 - (time.monotonic() - started))
                delay = max(delay, retry_at.get(target, 0) - time.monotonic())
                if delay > 0:
                    await asyncio.sleep(min(delay, 5))
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
                        gate = self.participation_gate(state, target)
                        fresh = any(int(row.get("seq") or 0) > observation.get("checked_seq", 0) for row in rows)
                        if not fresh or gate in {"paused", "disabled", "message_driven_disabled"}:
                            if observation.get("decision") in {"waiting", "deferred"}:
                                observation.update(decision="idle", reason="现场已结束，等待新的消息" if not fresh
                                                   else "当前自主接话已暂停")
                            pending[key].pop(target, None)
                            retry_at.pop(target, None)
                            continue
                        if gate:
                            self.note_participation_waiting(state, target, gate)
                            retry_at[target] = time.monotonic() + 5
                            continue
                considered = arrived
                task = self.queue_background("participation:" + key, lambda: self.maybe_participate(target, sample=True))
                if task is None:
                    retry_at[target] = time.monotonic() + 5
                    continue
                await asyncio.shield(task)
                async with self.session_state(target) as state:
                    observation = state.participation.get(target) or {}
                    choice = observation.get("opportunity") or {}
                    if observation.get("decision") == "deferred":
                        retry_at[target] = time.monotonic() + max(1, min(30, float(choice.get("due_at") or 0) - self._now()))
                        continue
                    if observation.get("decision") == "waiting":
                        retry_at[target] = time.monotonic() + 5
                        continue
                if pending.get(key, {}).get(target) == considered:
                    pending[key].pop(target, None)
                    retry_at.pop(target, None)
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
    async def maybe_participate(self, session_id: str, *, sample: bool = False, source: str = "message", browse_id: str = "") -> TickOutcome | None:
        if not self.is_enabled(session_id) or (source != "browse" and not self.chat_is_group(session_id)):
            return None
        outcome = TickOutcome(session_id=session_id, speech_kind="browse_reply" if source == "browse" else "participation", place=session_id)
        async with self.session_state(session_id) as state:
            window = self._participation_window(source)
            rows = self.live_chat(state, session_id, window=window)
            observation = dict(state.participation.get(session_id) or {})
            new = [r for r in rows if int(r.get("seq") or 0) > observation.get("checked_seq", 0)]
            if source == "browse":
                from hashlib import sha256
                from ..context_sources import browse_feeling_item
                request = self.context_request(state, session_id, task="participation", rows=rows)
                persona = await self._persona_text(session_id)
                request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
                if browse_feeling_item(state, request, browse_id) is None:
                    return None
            gate = self.participation_gate(state, session_id, source=source)
            if gate or not new:
                if source == "browse":
                    reasons = {"paused": "当前会话暂停了主动接话", "disabled": "当前会话未启用翻消息后接话",
                               "resting": "正在休息或精力不足，暂未进入接话判断", "busy": "手头活动需要先收尾，暂未进入接话判断",
                               "reply_pending": "本次未进入接话判断：当时有对话等待回复或正在处理",
                               "peer_cooldown": "刚与其他机器人连续聊过，先留白；有人带来新发言时再看"}
                    status = gate or ("already_considered" if rows else "no_scene")
                    reason = reasons.get(gate) or ("这段现场已判断过，等待新的消息" if rows
                                                  else "最近15分钟没有仍可回应的消息，不追答旧聊天")
                    self.note_browse_reply_gate(state, session_id, browse_id, status, reason)
                elif new:
                    # A reply/activity can start while this task waits for a
                    # background worker, after the settler's initial gate check.
                    self.note_participation_waiting(state, session_id, gate)
                return None
            if not self.chat_media_allowed(state, session_id, new):
                reason = "只有图片或表情，尚无与她相接的当前对话；先看不接话（未进入模型判断）"
                observation.update(decision="observing", reason=reason)
                state.participation[session_id] = observation
                if source == "browse":
                    self.note_browse_reply_gate(state, session_id, browse_id, "no_context", reason)
                return None
            # Scheduling is not a model decision. Only advance checked_seq once
            # admitted; deferred scenes remain available until their live expiry.
            state.participation[session_id] = observation
            if sample and not self.participation_sample(state, session_id, new, await self._persona_text(session_id)):
                observation.update(decision="deferred", reason=(observation.get("opportunity") or {}).get("reason", "等待新的现场"))
                return outcome
            if self.llm is None:
                observation.update(decision="unavailable", reason="没有可用的主模型，暂未判断是否接话")
                if source == "browse":
                    self.note_browse_reply_gate(state, session_id, browse_id, "unavailable", "没有可用的主模型，未能判断是否接话")
                return outcome
            now = self._now()
            observation.update(checked_seq=max(int(r.get("seq") or 0) for r in rows),
                               checked_at=now, evaluated_at=now, decision="considering", reason="正在考虑是否接话",
                               request_id=outcome.speech_receipt_batch)
            observation["attempts"] = [at for at in observation.get("attempts", []) if 0 <= now - at < 3600][-119:]
            if source == "message":
                observation["attempts"].append(now)
            self.decision_trace(session_id, "participation", "considering", "近期群聊已聚合，判断是否有话想接",
                                messages=len(rows))
            self.claim_chat_scene(outcome, session_id, rows, window=window, state=state, media_rows=new)
            try:
                message, reply_to = await self.participation_text(state, session_id,
                    source=source, browse_id=browse_id, outcome=outcome)
            except asyncio.CancelledError:
                self.finish_participation_observation(state, session_id, outcome.speech_receipt_batch,
                    "cancelled", "接话推理已取消，等待最新现场")
                raise
            except Exception as exc:
                self.finish_participation_observation(state, session_id, outcome.speech_receipt_batch,
                    "failed", f"接话判断未完成（{type(exc).__name__}）")
                raise
            if source == "browse" and not outcome.speech_decisions:
                reason = str((state.participation.get(session_id) or {}).get("reason") or "未取得有效的接话决定")
                self.note_browse_reply_gate(state, session_id, browse_id, "failed", reason)
            if (not message or self.participation_gate(state, session_id, source=source)
                    or not self.chat_media_allowed(state, session_id, new)
                    or self._now() >= outcome.chat_expires_at[session_id]):
                reason = "这轮继续旁听" if not message else "现场与权限已变化，本轮取消发送"
                if outcome.speech_decisions:
                    self.decision_trace(session_id, "participation", "listening" if not message else "cancelled", reason)
                    state.participation[session_id]["decision"] = "listening" if not message else "cancelled"
                    state.participation[session_id]["reason"] = reason
                outcome.notes.append(reason)
            else:
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
            if self.reply_busy(target):
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
