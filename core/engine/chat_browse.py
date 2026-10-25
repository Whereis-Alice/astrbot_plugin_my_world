"""A quiet daily activity. Read a snapshot, think once, commit scoped feelings only."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from uuid import uuid4

from ..chat_browse import expire_feelings, feeling_error, feeling_prompt, reading_people, reading_text, reading_view
from ..config_scope import session_scoped
from ..json_actions import extract_json_object
from ..model_calls import model_context
from ..models import CHAT_BROWSE_IDS


class ChatBrowseMixin:
    def _browse_identity(self) -> str:
        # Stable across clock/background contexts, unlike a per-turn life lease.
        config = self.world.model_dump(mode="json")
        # Revoking speech must stop the optional reply, not throw away a reading.
        for key in ("proactive_chat_paused", "proactive_session_whitelist", "life_share_session_whitelist"):
            config.pop(key, None)
        config["chat_browse"].pop("reply_enabled", None)
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _browse_sources(self, state) -> list[str]:
        config = self.world.chat_browse
        return [sid for sid in self.group_sessions(state.session_id) if self.is_enabled(sid)
                and (not config.sessions or sid in config.sessions)]

    def _browse_ready(self, state, action_id: str) -> bool:
        config, now = self.world.chat_browse, self._now()
        book = state.chat_browsing
        if (not config.enabled or state.is_sleeping or state.state == "drowsy" or state.energy < .25
                or (self.world.sleep.mode == "autonomous" and state.sleep_rhythm.get("decision") == "sleep")
                or self.life_choice(state)["kind"] in {"rest", "event", "schedule"}
                or now < float(book.get("next_at") or 0)
                or (book.get("day") == self._today_key(now) and int(book.get("count") or 0) >= config.daily_max)):
            return False
        return ((action_id == "browse_phone" and config.phone)
                or (action_id == "browse_computer" and state.node_id in config.computer_nodes))

    def maybe_start_chat_browse(self, state) -> bool:
        if state.current_action or state.current_plan:
            return False
        devices = [aid for aid in sorted(CHAT_BROWSE_IDS) if self._browse_ready(state, aid)]
        # The first suitable free moment after the minimum gap. Another random
        # 12% lottery made a two-hour gap silently turn into most of a day.
        if not devices:
            return False
        return self.start_chat_browse(state, self.rng.choice(devices))

    def chat_browse_status(self, state, session_id: str) -> dict:
        """Read-only diagnostics; completion requires a recorded thought."""
        config, now, book = self.world.chat_browse, self._now(), state.chat_browsing
        progress = (book.get("sessions") or {}).get(session_id) or {}
        count = int(book.get("count") or 0) if book.get("day") == self._today_key(now) else 0
        action = state.current_action or {}
        if not config.enabled:
            reason = "未开启空闲浏览"
        elif action.get("type") in CHAT_BROWSE_IDS:
            reason = "正在翻阅消息" if not action.get("inference_started") else "正在消化刚读过的消息"
        elif state.is_sleeping or state.state == "drowsy":
            reason = "正在休息，醒来后再看"
        elif state.energy < .25:
            reason = "精力偏低，先休息恢复"
        elif count >= config.daily_max:
            reason = "今天的浏览次数已用完"
        elif now < float(book.get("next_at") or 0):
            reason = "等待最短浏览间隔结束"
        elif action or state.current_plan:
            reason = "先做完手头的事，空闲时再看"
        elif self.life_choice(state)["kind"] in {"rest", "event", "schedule"} or (
                self.world.sleep.mode == "autonomous" and state.sleep_rhythm.get("decision") == "sleep"):
            reason = "先处理当前生活安排或休息"
        elif not (config.phone or state.node_id in config.computer_nodes):
            reason = "当前位置没有可用设备"
        elif not any(reading_view(state, sid, config, now) for sid in self._browse_sources(state)):
            reason = "目前没有可读的新消息"
        else:
            reason = "条件已就绪，下次空闲决策时浏览"
        return {"enabled": config.enabled, "reason": reason, "started_today": count,
            "daily_max": config.daily_max, "cooldown_minutes": config.cooldown_minutes,
            "next_at": float(book.get("next_at") or 0), "last_read_at": progress.get("at", 0),
            "completed_today": int(progress.get("completed") or 0) if progress.get("day") == self._today_key(now) else None,
            "last_result": progress.get("result", ""), "reply_enabled": config.reply_enabled,
            "last_error_at": float(book.get("last_error_at") or 0) if book.get("last_error_session") == session_id else 0,
            "reply_allowed": self.speech_allowed(session_id, "browse_reply"),
            "natural_silence": True,
            "shared": len(self.group_sessions(state.session_id)) > 1,
            "last_error": book.get("last_error", "") if book.get("last_error_session") == session_id else "",
            "note": "间隔是最短冷却，不是定时任务；空闲、有未读消息和可用设备时才浏览。每次成功读完都会留下想法，是否接话由她自己决定。"}

    def start_chat_browse(self, state, action_id: str) -> bool:
        if state.current_action or state.current_plan or not self._browse_ready(state, action_id):
            return False
        config, now = self.world.chat_browse, self._now()
        views = [(sid, reading_view(state, sid, config, now)) for sid in self._browse_sources(state)]
        views = [(sid, rows) for sid, rows in views if rows]
        if not views:
            return False
        progress = state.chat_browsing.get("sessions", {})
        sid, rows = min(views, key=lambda pair: float((progress.get(pair[0]) or {}).get("at", 0)))
        day = self._today_key(now)
        state.chat_browsing.update(day=day, count=(int(state.chat_browsing.get("count", 0)) if state.chat_browsing.get("day") == day else 0) + 1,
                                  next_at=now + config.cooldown_minutes * 60)
        self._settle_idle_practice(state)
        state.idle_activity = {}
        # No follow-on plan can turn this reading into queued speech or tools.
        state.current_plan = None
        state.current_action = {"type": action_id, "id": uuid4().hex, "desc": self.world.action_map()[action_id].name,
            "persona_key": self._matter_persona_key(state) or "",
            "duration_ticks": max(1, math.ceil(self.rng.randint(3, 8) * 60 / self.tick_seconds)), "elapsed_ticks": 0,
            "browse_session": sid, "browse_rows": rows, "started_at": now, "start_node": state.node_id,
            "identity": self._browse_identity(), "silent_followup": True}
        state.state = "browsing"
        return True

    def _browse_valid(self, state, action: dict) -> bool:
        sid = str(action.get("browse_session") or "")
        config = self.world.chat_browse
        persona_key = self._matter_persona_key(state)
        definition = self.world.action_map().get(str(action.get("type") or ""))
        if (action.get("type") not in CHAT_BROWSE_IDS or not action.get("id") or not action.get("browse_rows")
                or not config.enabled or not self.is_enabled(state.session_id)
                or definition is None or not definition.enabled or definition.idle or not definition.available_in(state.node_id)
                or (persona_key is not None and action.get("persona_key") and action["persona_key"] != persona_key)
                or state.node_id != action.get("start_node")
                or (action["type"] == "browse_phone" and not config.phone)
                or (action["type"] == "browse_computer" and state.node_id not in config.computer_nodes)
                or sid not in self._browse_sources(state) or state.current_action is None
                or state.current_action.get("id") != action.get("id")
                or self._browse_identity() != action.get("identity")
                or self._today_key(self._now()) != self._today_key(float(action.get("started_at") or 0))
                or self._now() - float(action.get("started_at") or 0) > 3600):
            return False
        current = {int(row.get("seq") or 0): row for row in state.recent_chat
                   if str(row.get("origin") or state.session_id) == sid and not row.get("internal")}
        trimmed = int(state.current_action.get("browse_trimmed_until") or 0)
        return bool(current) and all((row["seq"] not in current and row["seq"] <= trimmed)
                   or (row["seq"] in current and reading_text(current[row["seq"]]).startswith(row["text"]))
                   for row in action.get("browse_rows", []))

    def _stop_chat_browse(self, state, action: dict) -> None:
        if (action.get("type") in CHAT_BROWSE_IDS and action.get("id")
                and (state.current_action or {}).get("id") == action["id"]):
            # A normal chat/schedule may have queued a new plan while inference ran.
            state.current_action, state.state = None, "idle"

    def finish_chat_browse(self, state, action: dict) -> None:
        key = "chat_browse:" + state.session_id
        job = getattr(self, "_background_jobs", {}).get(key)
        if job and not job.done():
            return
        # An interrupted provider request is not replayed after a restart.
        if action.get("inference_started") or not self._browse_valid(state, action):
            self._stop_chat_browse(state, action)
            return
        self.queue_background(key, lambda: self._complete_chat_browse(state.session_id, action["id"]))

    @session_scoped
    async def _complete_chat_browse(self, session_id: str, action_id: str) -> None:
        completed = False
        async with self.session_state(session_id) as state:
            action = state.current_action or {}
            if action.get("id") != action_id:
                return
            if not self._browse_valid(state, action):
                self._stop_chat_browse(state, action)
                return
            action["inference_started"] = True
            snapshot = copy.deepcopy(action)
            persona = await self._persona_text(session_id)
            source = action["browse_session"]
            with self.config_scope(source):
                people = (reading_people(action["browse_rows"],
                    lambda uid: self.reading_person(source, uid))
                    if self.world.chat_browse.relationship_context and self.profiles.enabled() else [])
            prompt = json.dumps({"persona": persona, "activity": action["desc"],
                "place": self._node_name(state.node_id), "mood": state.mood, "energy": state.energy,
                "source_session": source, "people": people, "messages": action["browse_rows"]}, ensure_ascii=False)
        # Outside the state lock: ordinary chat and the basic clock keep working.
        try:
            with model_context("chat_browse", "日常翻阅近期消息，只产生内心感受", snapshot["browse_session"]):
                raw = await self._ask_judge(session_id,
                    feeling_prompt(), prompt, max_tokens=600)
            payload = extract_json_object(raw or "")
        except Exception:
            payload = None
        error = feeling_error(payload, snapshot["browse_rows"])
        # Repair a structured but invalid thought once, under the same budget.
        # Provider failures/denials do not trigger an extra request. No filler
        # thought is invented by the engine and no stale result is committed.
        if isinstance(payload, dict) and error:
            async with self.session_state(session_id) as state:
                if (await self._persona_text(session_id) != persona or not self._browse_valid(state, snapshot)):
                    self._stop_chat_browse(state, snapshot)
                    return
            try:
                with model_context("chat_browse", "补全本次阅读的想法与依据", snapshot["browse_session"]):
                    raw = await self._ask_judge(session_id, feeling_prompt(),
                        prompt + "\n上次结果未通过校验：" + error
                        + "。请重新依据这些消息写出非空的短暂想法和实际消息序号。", max_tokens=600)
                payload = extract_json_object(raw or "")
            except Exception:
                payload = None
            error = feeling_error(payload, snapshot["browse_rows"])
        async with self.session_state(session_id) as state:
            if (await self._persona_text(session_id) != persona or not self._browse_valid(state, snapshot)):
                self._stop_chat_browse(state, snapshot)
                return
            if isinstance(payload, dict) and isinstance(payload.get("inner"), str):
                completed = self._apply_browse_feeling(state, snapshot, payload, persona=persona)
            if not completed:
                state.chat_browsing["last_error_at"] = self._now()
                state.chat_browsing["last_error"] = error or "未能记录本次想法"
                state.chat_browsing["last_error_session"] = snapshot["browse_session"]
            self._stop_chat_browse(state, snapshot)

        # Reading and speaking have different scopes and authorization. Only the
        # source conversation's still-live scene may be considered, through the
        # same bounded response/quote path as normal participation, with a short
        # look-back after reading. No historical replay, tools or forced speech.
        target = snapshot["browse_session"]
        if completed:
            from ..browse_reply import BrowseReply, RETRYABLE, reply_status
            from .participation_queue import queue_browse
            async with self.session_state(session_id) as state:
                ticket = BrowseReply.capture(self, state, target, action_id)
            await self.maybe_participate(target, source="browse", browse_id=action_id, browse_ticket=ticket)
            async with self.session_state(session_id) as state:
                if reply_status(state, ticket) in RETRYABLE and queue_browse(self, ticket):
                    self.note_browse_reply_gate(state, target, action_id, "waiting",
                        f"先等当前回复或活动收尾，最多 {ticket.wait_seconds} 秒；原消息已回应或话题结束就放下")
                    thought = next((row for row in reversed(state.thoughts) if row.get("browse_id") == action_id), None)
                    if thought is not None:
                        thought["detail"]["browse_reply"]["until"] = ticket.deadline

    def _apply_browse_feeling(self, state, action: dict, payload: dict, *, persona: str = "") -> bool:
        now, sid = self._now(), action["browse_session"]
        rows = action["browse_rows"]
        if feeling_error(payload, rows):
            return False
        valid_seqs = {row["seq"] for row in rows}
        text = " ".join(payload["inner"].split())
        cited = payload["source_seqs"]
        progress = dict(state.chat_browsing.get("sessions") or {})
        previous = progress.get(sid) or {}
        day = self._today_key(now)
        progress[sid] = {"seq": max(valid_seqs), "at": now, "day": day,
            "completed": (int(previous.get("completed") or 0) if previous.get("day") == day else 0) + 1,
            "result": "feeling", "browse_id": action["id"]}
        state.chat_browsing["sessions"] = {key: value for key, value in progress.items() if key in self._browse_sources(state)}
        state.chat_browsing.pop("last_error_at", None)
        state.chat_browsing.pop("last_error", None)
        state.chat_browsing.pop("last_error_session", None)
        expire_feelings(state, now)
        quotes = [row for row in rows if row["seq"] in cited][:2]
        persona_key = hashlib.sha256(persona.encode("utf-8")).hexdigest()[:24]
        intent = payload.get("intent")
        intent = " ".join(intent.split())[:120] if isinstance(intent, str) else ""
        state.add_thought(text, source="翻消息时", persona_key=persona_key,
            detail={"env": self._node_name(state.node_id), "state": "browsing",
            "mood": state.mood, "intent": intent})
        thought = state.thoughts[-1]
        thought.update(at=now, browse_session=sid, browse_id=action["id"], browse_replied=False, expires_at=now + 24 * 3600,
                       browse_sources=[{"seq": row["seq"], "at": row["at"], "name": row["name"], "text": row["text"][:100]} for row in quotes])
        try:
            delta = float(payload.get("valence_delta") or 0)
        except (ValueError, TypeError):
            delta = 0
        if math.isfinite(delta):
            from ..state_dynamics import arousal_scale, valence_saturation
            self.dynamics._sync_emotions(state, now=now)
            scale = max(1, arousal_scale(state.affect) * valence_saturation(state.valence))
            self.dynamics.apply_pulse(state, {"valence": max(-.02, min(.02, delta)) / scale}, now=now)
            state.mood = self.dynamics.derive_mood(state)
        reason = str(payload.get("reason") or "").strip()[:160]
        if self.world.chat_browse.remember_meaningful and payload.get("meaningful") is True and reason:
            self.memory.remember(session_id=sid, persona_id="", node_id=state.node_id,
                content="翻阅消息时，我想到：" + text, memory_type="inner", source="chat_browse",
                scope="group", weight=.3, conflict_policy="skip_conflict",
                context=json.dumps({"kind": "chat_browse", "session": sid, "at": now, "reason": reason,
                                    "source_seqs": cited[:20], "browse_id": action["id"],
                                    "persona_key": persona_key}, ensure_ascii=False))
        return True

    def note_browse_reply_gate(self, state, target: str, browse_id: str, status: str, reason: str) -> None:
        """Explain an unattempted reply on the reading it belongs to, without inventing a choice."""
        thought = next((t for t in reversed(state.thoughts) if browse_id
                        and t.get("browse_id") == browse_id and t.get("browse_session") == target), None)
        if thought is not None:
            thought.setdefault("detail", {})["browse_reply"] = {
                "status": status, "reason": reason, "at": self._now(),
            }
