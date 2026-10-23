"""Shared life transitions and links; no additional model calls or task service."""
from __future__ import annotations

import copy
import hashlib
import json

from .. import life_matters as matters
from ..life_policy import admit_life
from ..planner import active_plan
from ..recovery import is_sleep_action, restores_energy


class LifeControlMixin:
    def _life_identity(self) -> str:
        from .life_inference import STATE_LEASE
        persona = (STATE_LEASE.get() or {}).get("persona", "")
        payload = json.dumps([self.world.model_dump(mode="json"), persona], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def admit_life(self, state, kind: str, *, critical: bool = False, force: bool = False, commit: bool = True) -> dict:
        fixed_drowsy = kind == "schedule" and self.world.sleep.mode == "fixed" and state.state == "drowsy"
        choice = admit_life(state, kind, critical=critical, force=force or fixed_drowsy)
        definition = self.world.action_map().get(str((state.current_action or {}).get("type") or ""))
        if kind == "schedule" and not force and (state.current_plan or {}).get("source") == "schedule":
            return {"allow": False, "interrupt": False, "reason": "另一项日程还在进行，先把它收尾"}
        if (kind == "schedule" and not force and state.current_action
                and state.life_decision.get("kind") == "schedule"
                and state.life_decision.get("at") == self._now()):
            return {"allow": False, "interrupt": False, "reason": "本轮已经开始一项日程，其他安排稍后再试"}
        if kind == "schedule" and definition and not definition.interruptible and not force:
            return {"allow": False, "interrupt": False, "reason": "当前活动需要先收尾，日程稍后再试"}
        if not choice["allow"]:
            return choice
        if choice["interrupt"] and commit:
            self.suspend_life(state, reason=choice["reason"], source=kind)
        return choice

    def suspend_life(self, state, *, reason: str, source: str) -> None:
        """Settle exactly once and retain a resumable plan, never a replayable tool call."""
        action, plan = state.current_action or {}, active_plan(state)
        self.settle_recovery_progress(state, action)
        self.pause_matter_action(state, action)
        matter_id = str(action.get("matter_id") or (plan or {}).get("matter_id") or "")
        item = matters.find(state, matter_id)
        if item and item["status"] not in matters.CLOSED | {"paused"}:
            item.update(status="active", obstacle=reason)
            matters.history(item, "interrupted", reason, self._now(), source=source,
                            step_id=action.get("matter_step_id", ""))
        # Only quiet, simulated actions may resume automatically. Tools, commands,
        # speech and expired plans are never replayed after an interruption.
        if not matter_id and not state.life_resume:
            if not plan and action:
                from ..planner import create_plan
                plan = create_plan(steps=[{"action": action.get("type"), "duration": int(action.get("duration_ticks", 1)) * self.tick_seconds,
                    "intent": action.get("intent", ""), "content": action.get("content", ""), "params": action.get("params", {})}],
                    world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds, source="life_resume")
            defs = self.world.action_map()
            pending = list((plan or {}).get("steps", []))[int((plan or {}).get("current_step", 0)):]
            if pending and all((d := defs.get(str(step.get("action") or "")))
                               and d.llm_level == "template" and d.target_type == "none"
                               and not is_sleep_action(d) and not restores_energy(d)
                               and d.id not in {"sleep", "nap", "walk_to", "say", "share", "poke", "browse_phone", "browse_computer"}
                               and d.on_complete.trigger == "none" for step in pending):
                saved = copy.deepcopy(plan)
                if action:
                    saved["steps"][int(plan.get("current_step", 0))]["duration"] = max(
                        1, (int(action.get("duration_ticks", 1)) - int(action.get("elapsed_ticks", 0))) * self.tick_seconds)
                state.life_resume = {"plan": saved, "at": self._now(), "node": state.node_id,
                                     "reason": reason, "source": source, "identity": self._life_identity()}
        state.current_action, state.current_plan, state.state = None, None, "idle"
        state.life_decision = {"kind": source, "reason": reason, "at": self._now(), "matter_id": matter_id}
        matters.sync(state)

    def resume_life(self, state) -> bool:
        saved = state.life_resume
        if not saved or state.current_action or state.current_plan or state.is_sleeping or state.state == "drowsy":
            return False
        if self.life_choice(state)["kind"] not in {"idle", "matter"}:
            return False
        # A day-old intention or a different location deserves a new decision.
        if self._now() - float(saved.get("at", 0)) > 86400 or saved.get("node") != state.node_id:
            state.life_resume = {}
            return False
        if saved.get("identity") != self._life_identity():
            state.life_resume = {}
            return False
        plan = saved.get("plan") or {}
        if float(plan.get("expires_at") or 0) and self._now() >= float(plan["expires_at"]):
            state.life_resume = {}
            return False
        if any(not (d := self.world.action_map().get(str(s.get("action") or ""))) or not d.enabled
               or d.id in {"browse_phone", "browse_computer"}
               for s in plan.get("steps", [])):
            state.life_resume = {}
            return False
        state.current_plan, state.life_resume = plan, {}
        state.life_decision = {"kind": "continue", "reason": "临时安排已结束，接着做刚才放下的事", "at": self._now()}
        return True

    async def link_event_matter(self, state, thread: dict, proposal: dict) -> None:
        """Explicit, optional continuation from a settled event; one link per thread."""
        if (not self.world.limits.life_matter_enabled or thread.get("matter_linked")
                or not isinstance(proposal, dict)):
            return
        session = str(thread.get("session") or state.session_id)
        _, persona_key = await self._journal_persona(session)
        target = matters.find(state, str(proposal.get("matter_id") or ""))
        if proposal.get("matter_id") and not target:
            return
        if target and str(target.get("persona_key") or "") != persona_key:
            return
        text, reason = str(proposal.get("text") or "").strip(), str(proposal.get("reason") or "").strip()
        if not reason:
            return
        if target:
            if target["status"] in matters.CLOSED | {"paused"} or not matters.visible(target, session):
                return
            matters.history(target, "event_link", reason, self._now(), event_id=thread["id"])
        elif text:
            # Events can create personal intentions, never promises to real users.
            target = self.add_life_matter(
                state,
                text,
                session=session,
                kind="idea",
                motivation=reason,
                persona_key=persona_key,
            )
            if not target:
                return
            target["origin_event"] = str(thread["id"])
            matters.history(target, "event_link", reason, self._now(), event_id=thread["id"])
        else:
            return
        thread["matter_linked"] = target["id"]
        target["event_links"] = list(dict.fromkeys([*target.get("event_links", []), str(thread["id"])]))[-12:]
        matters.sync(state)

    async def run_matter_appointment(self, state, schedule, *, session: str, force: bool = False) -> bool:
        if not self.world.limits.life_matter_enabled:
            return False
        item = matters.find(state, schedule.matter_id)
        if (not item or not matters.visible(item, session) or item["status"] in matters.CLOSED | {"paused"}
                or item.get("blocker") == "evidence" or float(item.get("resume_at") or 0) > self._now()):
            return False
        if (state.current_action or {}).get("matter_id") == item["id"]:
            return True
        step = next((s for s in item.get("steps", []) if s.get("status") != "done"), None)
        if item.get("needs_replan") or item.get("needs_review") or not step or float(step.get("not_before") or 0) > self._now():
            return False
        day = self._today_key(self._now())
        if item.get("work_day") != day:
            item.update(work_day=day, worked_minutes=0, plan_attempts=0)
        if item.get("worked_minutes", 0) >= self.world.limits.life_matter_daily_minutes:
            return False
        if (step["action"], step["node"]) not in {(row["action"], row["node"]) for row in self._matter_actions(state)}:
            return False
        thread = self._active_thread(state)
        if self._thread_critical(thread) and not force:
            return False
        if not self.admit_life(state, "schedule", force=force)["allow"]:
            return False
        await self._start_matter_step(state, item)
        started = (state.current_action or {}).get("matter_id") == item["id"] or (state.current_plan or {}).get("matter_id") == item["id"]
        if started:
            matters.history(item, "appointment", f"按日程「{schedule.note or schedule.id}」接着做", self._now(), schedule_id=schedule.id)
        return started

    def life_overview(self, state, session: str) -> dict:
        now = self.local_now()
        day, weekday = now.date().isoformat(), self.WEEKDAY_KEYS[now.weekday()]
        agenda = []
        for schedule in (self.schedules.schedules if self.schedules else []):
            if (not schedule.enabled or not self.schedule_in_scope(schedule, session)
                    or (schedule.once and schedule.date and schedule.date != day)
                    or (not schedule.once and schedule.days and weekday not in schedule.days)):
                continue
            item = matters.find(state, schedule.matter_id) if schedule.matter_id else None
            if item and not matters.visible(item, session):
                continue
            stamp = self._schedule_moment(now, schedule)
            delay = state.schedule_delays.get(schedule.id) or {}
            fired = float(state.schedule_last_fired.get(schedule.id) or 0)
            status = "upcoming"
            if delay.get("day") == day and delay.get("cancelled"):
                status = "cancelled"
            elif delay.get("day") == day and float(delay.get("until") or 0) > 0:
                status = "delayed"
            elif stamp and fired >= stamp.timestamp():
                status = "started"
            elif stamp and stamp < now:
                status = "past"
            agenda.append({"id": schedule.id, "time": schedule.time, "title": item["text"] if item else schedule.note or " · ".join(self._chain_labels(schedule.action_chain)) or schedule.id,
                           "matter_id": schedule.matter_id, "status": status, "at": stamp.timestamp() if stamp else 0,
                           "retry_at": float(delay.get("until") or 0), "note": schedule.note})
        experiences = [{"id": row.get("id"), "title": row.get("title") or "生活中的一件事",
                        "status": row.get("status"), "matter_id": row.get("matter_linked", ""),
                        "next": row.get("pending_followup", ""), "at": row.get("updated_at") or row.get("started_at") or row.get("created_at") or 0,
                        "result": str((row.get("steps") or [{}])[-1].get("result") or "")[:200]}
                       for row in state.event_threads[-12:] if (row.get("session") or state.session_id) == session]
        return {"day": day, "agenda": sorted(agenda, key=lambda row: (row["time"], row["id"])),
                "experiences": list(reversed(experiences)), "rhythms": matters.routine_hints(state, now=self._now()),
                "resume": {"reason": state.life_resume.get("reason"), "at": state.life_resume.get("at")} if state.life_resume else {}}
