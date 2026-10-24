"""Shared life transitions and links; no additional model calls or task service."""
from __future__ import annotations

import copy
import hashlib
import json

from .. import life_matters as matters
from ..life_policy import admit_life
from ..planner import active_plan, advance, same_plan
from ..recovery import is_sleep_action, restores_energy


class LifeControlMixin:
    def _running_life_belongs(self, state, record: dict) -> bool:
        """Legacy unmarked work remains compatible; marked work owns one character."""
        key = self._matter_persona_key(state)
        if record.get("persona_key") and record["persona_key"] != key:
            return False
        item = matters.find(state, str(record.get("matter_id") or ""))
        return item is None or matters.belongs_to(item, key)

    def _running_action_invalid_reason(self, state, action: dict) -> str:
        definition = self.world.action_map().get(str(action.get("type") or ""))
        if not self._running_life_belongs(state, action):
            return "人设已变化，停止原角色的动作"
        if definition is None or not definition.enabled or definition.idle:
            return "动作已删除、停用或改为闲置，停止执行"
        if action.get("type") == "walk_to" and action.get("target_node") and action["target_node"] not in self.world.node_map():
            return "移动目的地已删除，停止执行"
        if definition.id != "sleep" and not definition.available_in(state.node_id):
            return f"动作的地点限制已变化，在 {state.node_id} 停止执行"
        return ""

    async def _validate_running_life(self, state, outcome=None) -> None:
        """Stop invalid work before any new time, completion effects or queued speech."""
        if state.idle_activity and not self._running_life_belongs(state, state.idle_activity):
            state.idle_activity = {}
            await self._log_event(state, "skip", {"note": "人设已变化，停止原角色的闲置活动"}, outcome=outcome)
        plan = state.current_plan or {}
        if plan and not self._running_life_belongs(state, plan):
            state.current_plan = None
            if state.state == "drowsy" and not state.current_action:
                self._cancel_pending_sleep(state, reason="人设已变化，重新评估休息", source="character_change", postpone=False)
            await self._log_event(state, "skip", {"plan_id": plan.get("id", ""),
                "note": "人设已变化，停止原角色的排队计划"}, outcome=outcome)
        action = state.current_action
        if not action:
            return
        definition = self.world.action_map().get(str(action.get("type") or ""))
        reason = self._running_action_invalid_reason(state, action)
        if not reason:
            return
        # Only already elapsed online ticks are retained. No completion bonus,
        # model followup, tool replay or newly elapsed recovery is awarded.
        self.pause_matter_action(state, action)
        if is_sleep_action(definition):
            self._finish_sleep_rhythm(state, action, interrupted=True)
        plan = state.current_plan
        if plan and action.get("from_plan") and same_plan(action, plan):
            index = self._running_plan_index(state, plan)
            if index is None and not action.get("plan_id") and not plan.get("id"):
                index = int(plan.get("current_step") or 0)
            if index is not None and index < len(plan.get("steps", [])):
                if index == int(plan.get("current_step") or 0):
                    advance(state, status="skipped")
                else:
                    plan["steps"][index]["status"] = "skipped"
            if plan.get("matter_id") and plan.get("matter_id") == action.get("matter_id"):
                state.current_plan = None
        state.current_action, state.state = None, "idle"
        state.pending_arrival = False
        await self._log_event(state, "skip", {"action": action.get("type"), "note": reason}, outcome=outcome)

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
            saved = self._bookmark_quiet_life(state, action, plan)
            if saved:
                state.life_resume = {"plan": saved, "at": self._now(), "node": state.node_id,
                                     "reason": reason, "source": source, "identity": self._life_identity()}
        state.current_action, state.current_plan, state.state = None, None, "idle"
        state.life_decision = {"kind": source, "reason": reason, "at": self._now(), "matter_id": matter_id}
        matters.sync(state)

    def _quiet_resume_step(self, step: dict) -> bool:
        definition = self.world.action_map().get(str(step.get("action") or ""))
        return bool(definition and definition.enabled and definition.llm_level == "template"
                    and definition.target_type == "none" and not self._action_speaks(definition)
                    and not step.get("target") and not step.get("interject")
                    and not is_sleep_action(definition) and not restores_energy(definition)
                    and definition.id not in {"walk_to", "say", "share", "poke", "think", "browse_phone", "browse_computer"}
                    and definition.on_complete.trigger == "none")

    def _bookmark_quiet_life(self, state, action: dict, plan: dict | None) -> dict | None:
        """Keep running work before its untouched tail; a tail is not the running step."""
        from ..planner import create_plan
        saved = copy.deepcopy(plan) if plan else None
        pending = self._plan_remaining_steps(plan)
        current = max(0, int((plan or {}).get("current_step") or 0))
        if action:
            index = self._running_plan_index(state, plan)
            # Older hand-built archives have no execution/plan linkage.
            if index is None and "from_plan" not in action and not action.get("plan_id") and pending:
                if pending[0].get("action") == action.get("type"):
                    index = current
            if index is not None and index >= current and index - current < len(pending):
                running = pending.pop(index - current)
            else:
                running = self._step_payload(self._step_to_action({"action": action.get("type"),
                    "intent": action.get("intent", ""), "content": action.get("content", ""),
                    "target": action.get("target", ""), "target_node": action.get("target_node", ""),
                    "params": action.get("params", {})}), session=action.get("session", ""),
                    kind=action.get("speech_kind", ""))
            ticks = max(1, int(action.get("duration_ticks", 1)) - int(action.get("elapsed_ticks", 0)))
            running["duration"] = ticks * self.tick_seconds
            usage_keys = action.get("usage_keys")
            running["_life_resume"] = {"ticks": ticks,
                "usage_keys": dict(usage_keys) if isinstance(usage_keys, dict) else {}}
            pending.insert(0, running)
        if not pending or not all(self._quiet_resume_step(step) for step in pending):
            return None
        if saved:
            saved["steps"] = saved["steps"][:current] + pending
        else:
            saved = create_plan(steps=pending, world_time=state.world_time, now=self._now(),
                tick_seconds=self.tick_seconds, source="life_resume",
                persona_key=action.get("persona_key") or self._matter_persona_key(state) or "")
            # The normal plan constructor deliberately strips internal receipts.
            saved["steps"][0]["_life_resume"] = pending[0]["_life_resume"]
        return saved

    def _planned_life_resumption(self, state, definition, action) -> dict | None:
        """Only a validated bookmark's actual step may override a new-start duration."""
        plan = state.current_plan or {}
        identity = plan.get("_life_resume_identity")
        if not identity or identity != self._life_identity():
            return None
        steps, index = plan.get("steps") or [], max(0, int(plan.get("current_step") or 0))
        if index >= len(steps):
            return None
        step = steps[index]
        if action.raw is not step or step.get("action") != definition.id or not self._quiet_resume_step(step):
            return None
        receipt = step.get("_life_resume")
        if not isinstance(receipt, dict) or not isinstance(receipt.get("ticks"), int) or receipt["ticks"] <= 0:
            return None
        return {"ticks": receipt["ticks"], "usage_keys": receipt.get("usage_keys")
                if isinstance(receipt.get("usage_keys"), dict) else {}}

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
        plan["_life_resume_identity"] = saved["identity"]
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
        if (not item or not matters.visible(item, session)
                or not matters.belongs_to(item, self._matter_persona_key(state))
                or item["status"] in matters.CLOSED | {"paused"}
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
                       for row in self._event_threads(state)[-12:] if (row.get("session") or state.session_id) == session]
        return {"day": day, "agenda": sorted(agenda, key=lambda row: (row["time"], row["id"])),
                "experiences": list(reversed(experiences)), "rhythms": matters.routine_hints(state, now=self._now(),
                    persona_key=self._matter_persona_key(state)),
                "resume": {"reason": state.life_resume.get("reason"), "at": state.life_resume.get("at")} if state.life_resume else {}}
