"""Life projects: snapshot -> inference -> guarded commit -> timed in-world activity."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from .. import life_matters as matters
from ..config_scope import session_scoped
from ..json_actions import extract_json_object
from ..life import life_role
from ..life_policy import choose_life
from ..models import LIFE_SIMULATION_ACTION
from ..model_calls import model_context
from ..pathfinding import find_path
from ..planner import create_plan
from ..state import WorldState
from .models import TickOutcome


class MattersMixin:
    def life_choice(self, state) -> dict:
        thread = self._active_thread(state)
        schedules = [self._next_schedule(sid) for sid in self.group_sessions(state.session_id)]
        minutes = [row["in_minutes"] for row in schedules if row is not None]
        return choose_life(state, now=self._now(), critical=bool(thread and self._thread_critical(thread)),
                           schedule_minutes=min(minutes) if minutes else None,
                           sleep_requested=self.world.sleep.mode == "autonomous")

    def _matter_can_work(self, state) -> bool:
        return state.state == "idle" and self.life_choice(state)["kind"] in {"idle", "matter"}

    def life_seed_due(self, state: WorldState) -> bool:
        """One suggestion a day; transient failures get a bounded, delayed retry."""
        if (not self.world.limits.life_matter_enabled or not self.world.limits.life_matter_self_start
                or not self._matter_can_work(state) or state.world_time * self.tick_seconds < 6 * 3600
                or any(m["status"] in {"active", "working"} for m in state.life_matters)):
            return False
        key = f"life_seed:{self.state_key(state.session_id)}:{self._today_key(self._now())}"
        marker = self.db.raw.kv_get(key)
        if not marker:
            return True
        # A legacy marker already consumed this day's suggestion.
        return (isinstance(marker, dict) and "attempts" in marker and not marker.get("done")
                and int(marker["attempts"]) < 3 and float(marker.get("retry_at") or 0) <= self._now())

    @session_scoped
    async def seed_life_matter(self, session_id: str) -> None:
        """At most one quiet personal suggestion per day, after her first six lived hours."""
        if not self.is_enabled(session_id) or not self.world.limits.life_matter_enabled or not self.world.limits.life_matter_self_start:
            return
        world = self.world
        state_key = self.state_key(session_id)
        day = self._today_key(self._now())
        key = f"life_seed:{state_key}:{day}"
        async with self.session_state(session_id) as state:
            matters.ensure(state, self._now())
            if not self.life_seed_due(state):
                return
            candidates = self._matter_actions(state)
            interests = matters.stable_interests(state, now=self._now())
            marker = self.db.raw.kv_get(key) or {}
            attempts = int(marker.get("attempts") or 0) + 1
            self.db.raw.kv_set(key, {"at": self._now(), "attempts": attempts,
                "retry_at": self._now() + max(15, self.world.limits.life_matter_interval_minutes) * 60})
        persona = await self._persona_text(session_id)
        from hashlib import sha256
        persona_key = sha256(persona.encode("utf-8")).hexdigest()[:24]
        # A new day is not a blank life. Keep only her personal projects;
        # conversation promises and another persona's history stay private.
        recent = [m for m in state.life_matters if not m.get("who") and m.get("kind") != "promise"
                  and (not m.get("persona_key") or m["persona_key"] == persona_key)
                  and (m["status"] not in matters.CLOSED
                       or self._now() - float(m.get("updated_at") or m.get("at") or 0) <= 30 * 86400)]
        previous = [{"text": m["text"], "status": m["status"], "motivation": m.get("motivation", ""),
                     "next_step": m.get("next_step", ""), "result": m.get("result", ""),
                     "obstacle": m.get("obstacle", "")} for m in recent if m["status"] not in matters.CLOSED]
        closed = sorted((m for m in recent if m["status"] in matters.CLOSED),
                        key=lambda m: float(m.get("updated_at") or m.get("at") or 0))[-12:]
        previous += [{"text": m["text"], "status": m["status"], "result": m.get("result", "")}
                     for m in closed]
        with model_context("life_matter", "有空闲，为自己的生活想一件可持续的小事", session_id):
            raw = await self._ask_judge(session_id,
                '结合人设和已有能力，想一件愿意持续几天的独处小事。只输出JSON：{"text":"", "motivation":""}。'
                '没有想法可以留空。只在角色世界内模拟生活，不建立新身世、恋爱关系或重大物品。'
                '先看recent_matters：已完成的事情不从头重建，暂停或等待的事情仍由原事项接续。'
                '把旧目标换种说法、细化下一步、比较做法或记录效果，都属于原事项，不能另起一个新项目。'
                '新念头须有不同的具体方向，可以由已有经历延伸；没有新方向就留空，不必每天建立项目。'
                '编程、创作等兴趣可在世界内模拟，不要求配置电脑，不声称操作了现实文件或外部服务。不要声称已经做成。',
                json.dumps({"persona": persona, "available": candidates,
                            "interests": interests, "recent_matters": previous}, ensure_ascii=False))
        payload = extract_json_object(raw or "") or {}
        if "text" not in payload or not isinstance(payload["text"], str):
            # Backoff begins when the request returns, even after a slow failure.
            self.db.raw.kv_set(key, {"at": self._now(), "attempts": attempts,
                "retry_at": self._now() + max(15, world.limits.life_matter_interval_minutes) * 60})
            return  # Unavailable/malformed is different from a valid "no new idea".
        if self.world is not world or self.state_key(session_id) != state_key or not self.is_enabled(session_id):
            return
        async with self.session_state(session_id) as state:
            current_persona = await self._persona_text(session_id)
            if (current_persona != persona or self.world is not world or state.session_id != state_key
                    or not self.is_enabled(session_id) or not self.world.limits.life_matter_enabled
                    or not self.world.limits.life_matter_self_start or self._today_key(self._now()) != day):
                return
            matters.ensure(state, self._now())
            if (not self._matter_can_work(state)
                    or any(m["status"] in {"active", "working"} for m in state.life_matters)):
                return
            self.db.raw.kv_set(key, {"at": self._now(), "attempts": attempts, "done": True})
            text = " ".join(payload["text"].split())[:160]
            # Exact repeats are a valid "no new idea", not a second project.
            # Avoid semantic fuzzy merging: similar goals can have different meanings.
            def normalize(value: str) -> str:
                return "".join(c for c in str(value).casefold() if c.isalnum())
            if any(normalize(m["text"]) == normalize(text) for m in state.life_matters
                   if not m.get("who") and m.get("kind") != "promise"
                   and (not m.get("persona_key") or m["persona_key"] == persona_key)
                   and (m["status"] not in matters.CLOSED
                        or self._now() - float(m.get("updated_at") or m.get("at") or 0) <= 30 * 86400)):
                return
            _, persona_key = await self._journal_persona(session_id)
            self.add_life_matter(state, text, session=session_id,
                                 motivation=str(payload.get("motivation") or ""),
                                 persona_key=persona_key)

    def confirm_matter_from_turn(self, state, ctx, matter_id: str, evidence: str, trace: list) -> bool:
        matters.ensure(state, self._now())
        item = matters.find(state, matter_id)
        if not item or item.get("session") != ctx.session_id or (item.get("who") and item["who"] != ctx.user_id):
            return False
        # An exact observed success can ground a conclusion; a character's claim cannot.
        failure_words = ("失败", "未发送", "没有发送", "未完成", "超时", "取消", '"ok": false', '"success": false', "error:", "failed")
        receipts = [row for row in trace if row.get("ok") is True and len(evidence.strip()) >= 4
                    and evidence in str(row.get("result") or "")
                    and not any(word in str(row.get("result") or "").lower() for word in failure_words)]
        if not receipts:
            return False
        done = self.mark_own_topic_done(state, user_id=ctx.user_id, matter_id=matter_id,
                                         evidence=evidence, confirmed=True)
        if done:
            matters.history(item, "tool_evidence", evidence, self._now(), receipt=receipts[0])
        return bool(done)

    def reconcile_matters(self, state) -> None:
        matters.ensure(state, self._now())
        running = str((state.current_action or {}).get("matter_id") or (state.current_plan or {}).get("matter_id") or "")
        for item in state.life_matters:
            if item["status"] == "working" and item["id"] != running:
                item.update(status="active", obstacle="上次的小步被打断，稍后接着做")
                matters.history(item, "interrupted", item["obstacle"], self._now())
        matters.sync(state)

    def settle_matter_work(self, state, action: dict, *, at: float | None = None) -> float:
        """Settle online ticks once, assigning midnight work to its actual day."""
        item = matters.find(state, str(action.get("matter_id") or ""))
        if item is None or action.get("type") == "walk_to":
            return 0
        step = next((s for s in item.get("steps", []) if s["id"] == action.get("matter_step_id")), None)
        if not step or step.get("status") == "done":
            return 0
        elapsed = max(0, int(action.get("elapsed_ticks") or 0))
        accounted = max(0, int(action.get("matter_accounted_ticks") or 0))
        total = 0.0
        # Usually one tick; legacy running saves may contain several unsettled ticks.
        for tick in range(accounted + 1, elapsed + 1):
            day = self._today_key((self._now() if at is None else at) - (elapsed - tick + .5) * self.tick_seconds)
            if item.get("work_day") != day:
                item.update(work_day=day, worked_minutes=0, plan_attempts=0)
            minutes = min(self.tick_seconds / 60,
                          max(0, step["minutes"] - step.get("worked_minutes", 0)),
                          max(0, self.world.limits.life_matter_daily_minutes - item.get("worked_minutes", 0)))
            if minutes <= 0:
                continue
            item["worked_minutes"] = item.get("worked_minutes", 0) + minutes
            step["worked_minutes"] = step.get("worked_minutes", 0) + minutes
            total += minutes
        action["matter_accounted_ticks"] = elapsed
        if total:
            matters.capture_bookmark(item, step, now=self._now() if at is None else at)
            matters.sync(state)
        return total

    def pause_matter_action(self, state, action: dict, *, at: float | None = None) -> None:
        self.settle_life_practice(state, action, at=at)
        settled = self.settle_matter_work(state, action, at=at)
        item = matters.find(state, str(action.get("matter_id") or ""))
        step = next((s for s in (item or {}).get("steps", []) if s["id"] == action.get("matter_step_id")), None)
        if item and step:
            matters.capture_bookmark(item, step, now=self._now() if at is None else at, reason="暂时放下，保留当前切入点")
        if settled:
            matters.history(item, "paused_step", f"暂时放下，已经投入 {step['worked_minutes']:g} 分钟", self._now())

    def _stop_matter_work(self, state: WorldState, matter_id: str) -> None:
        """Release only this matter's preparation, preserving confirmed online effort."""
        action = state.current_action or {}
        if action.get("matter_id") == matter_id:
            self.pause_matter_action(state, action)
            state.current_action = None
            state.state = "idle"
        if (state.current_plan or {}).get("matter_id") == matter_id:
            state.current_plan = None

    def add_life_matter(self, state, text: str, **kwargs) -> dict | None:
        matters.ensure(state, self._now())
        body = " ".join(str(text or "").split())[:160]
        if not body:
            return None
        who = str(kwargs.get("who") or "")
        session = str(kwargs.pop("session", "") or state.session_id)
        persona_key = str(kwargs.get("persona_key") or "")
        existing = next((m for m in state.life_matters if m["text"] == body and m.get("who") == who
                         and str(m.get("persona_key") or "") == persona_key
                         and m.get("session") == session and m["status"] not in matters.CLOSED), None)
        if existing:
            return existing
        if sum(m["status"] not in matters.CLOSED | {"paused"} for m in state.life_matters) >= self.world.limits.life_matter_active_max:
            return None
        if sum(m["status"] == "paused" for m in state.life_matters) >= 20:
            return None
        item = matters.make(body, now=self._now(), session=session, **kwargs)
        matters.history(item, "created", "记下一件想继续做的事", self._now())
        state.life_matters.append(item)
        matters.sync(state)
        return item

    def manage_life_matter(self, state, matter_id: str, operation: str, *, reason: str = "",
                           resume_at: float = 0, **changes: Any) -> dict:
        matters.ensure(state, self._now())
        item = matters.find(state, matter_id)
        if item is None:
            raise ValueError("找不到这件生活事项")
        if operation in {"pause", "resume", "postpone", "abandon"}:
            if item["status"] in matters.CLOSED:
                raise ValueError("已经结束的事项不能再次执行，请新建事项")
            if operation in {"resume", "postpone"} and item["status"] == "paused" and sum(
                m["status"] not in matters.CLOSED | {"paused"} for m in state.life_matters
            ) >= self.world.limits.life_matter_active_max:
                raise ValueError("进行中的生活事项已达上限，请先暂停或结束其他事项")
            item["status"] = {"pause": "paused", "resume": "active", "postpone": "waiting", "abandon": "abandoned"}[operation]
            item["resume_at"] = resume_at if operation == "postpone" else 0
            item["obstacle"] = str(reason)[:300]
        elif operation == "complete":
            if not reason.strip():
                raise ValueError("手动确认完成时请填写实际结果")
            item.update(status="completed", progress=100, result=reason[:500], completed_at=self._now())
        elif operation == "edit":
            if item["status"] in matters.CLOSED:
                raise ValueError("已经结束的事项保留为历史，请新建事项")
            if "text" in changes and not str(changes["text"]).strip():
                raise ValueError("事项内容不能为空")
            changed_goal = "text" in changes and str(changes["text"]).strip() != item["text"]
            if changed_goal:
                for row in [*item.get("stage_notes", []), *item.get("steps", [])]:
                    row.setdefault("goal", item["text"])
            for key in ("text", "motivation", "next_step"):
                if key in changes:
                    item[key] = str(changes[key]).strip()[:160 if key == "text" else 300]
            if changed_goal:
                item.update(needs_replan=True, needs_review=False, last_attempt_at=0, plan_attempts=0,
                            resume_at=0, obstacle="", blocker="",
                            next_step="目标已调整，保留已做步骤，重新安排后续")
        else:
            raise ValueError("支持暂停、继续、延期、放弃、编辑和手动确认结果")
        if operation in {"pause", "postpone", "abandon", "complete", "edit"}:
            self._stop_matter_work(state, matter_id)
        if operation == "resume":
            if item.get("blocker") == "evidence":
                item.update(needs_replan=True, needs_review=False)
            item.update(last_attempt_at=0, plan_attempts=0, blocker="")
        if operation == "edit" and item["status"] == "working":
            item["status"] = "active"
        matters.history(item, "manual_" + operation, reason or "管理员调整", self._now(), manual=True)
        matters.sync(state)
        return item

    def _matter_actions(self, state) -> list[dict]:
        rows = [{"action": LIFE_SIMULATION_ACTION.id, "name": "世界内模拟活动",
                 "description": LIFE_SIMULATION_ACTION.description, "node": state.node_id}]
        for action in self.world.actions:
            # Personal quiet work cannot send messages, touch a person or replay external tools.
            if (not action.enabled or action.idle or action.id == "walk_to" or action.category != "continuous"
                    or action.llm_level != "template" or action.target_type != "none"
                    or action.tool_list() or action.trigger_command or action.on_complete.trigger not in {"none", "llm_followup"}
                    or life_role(action) not in {"interest", "explore"}):
                continue
            for node in self.world.nodes:
                if node.id != state.node_id and not self.decider.action_usable("walk_to"):
                    continue
                if action.available_in(node.id) and (node.id == state.node_id or find_path(self.world.adjacent(), state.node_id, node.id)):
                    if self.action_quota_left(state, action) and self._check_preconditions(state, action)[0]:
                        rows.append({"action": action.id, "name": action.name, "description": action.description[:250], "node": node.id})
        return rows[:80]

    @session_scoped
    async def advance_life_matters(self, session_id: str) -> None:
        if not self.is_enabled(session_id) or not self.world.limits.life_matter_enabled:
            return
        world, state_key = self.world, self.state_key(session_id)
        now = self._now()
        async with self.session_state(session_id) as state:
            self.reconcile_matters(state)
            if not self._matter_can_work(state):
                return
            day = self._today_key(now)
            eligible = []
            for candidate in state.life_matters:
                if candidate["status"] not in {"active", "waiting", "blocked", "working"}:
                    continue
                if candidate.get("blocker") == "evidence":
                    continue
                if candidate.get("work_day") != day:
                    candidate.update(work_day=day, worked_minutes=0, plan_attempts=0)
                if (float(candidate.get("resume_at") or 0) > now
                        or now - float(candidate.get("last_attempt_at") or 0) < self.world.limits.life_matter_interval_minutes * 60
                        or candidate.get("worked_minutes", 0) >= self.world.limits.life_matter_daily_minutes):
                    continue
                needs_inference = not candidate.get("steps") or candidate.get("needs_replan") or candidate.get("needs_review")
                if needs_inference and int(candidate.get("plan_attempts", 0)) >= 3:
                    continue
                eligible.append(candidate)
            if not eligible:
                return
            item = min(eligible, key=lambda m: (m.get("kind") != "promise", float(m.get("last_attempt_at") or 0), float(m.get("at") or 0)))
            item["last_attempt_at"] = now
            review = bool(item.get("needs_review"))
            if item.get("steps") and not item.get("needs_replan") and not review:
                await self._start_matter_step(state, item)
                return
            item["plan_attempts"] = int(item.get("plan_attempts", 0)) + 1
            snapshot = copy.deepcopy(item)
            snapshot["stage_notes"] = matters.current_stage_notes(item)[-3:]
            snapshot["steps"] = [s for s in item.get("steps", []) if s.get("status") == "done"][-12:] + [s for s in item.get("steps", []) if s.get("status") != "done"][:6]
            snapshot["history"] = snapshot.get("history", [])[-12:]
            interrupted = [event for event in item.get("history", []) if event.get("kind") == "paused_step"
                           and now - float(event.get("at") or 0) <= 7 * 86400]
            snapshot["rhythm_hint"] = "最近多次被打断，优先拆成5到15分钟的小步；不代表喜好改变" if len(interrupted) >= 2 else ""
            candidates = self._matter_actions(state)
        # Never wait for inference while holding the life-state lock.
        persona = await self._persona_text(session_id)
        if not await self._matter_context_current(session_id, world, state_key, persona, day=day):
            return
        if review:
            await self._review_matter_stage(session_id, snapshot, world=world, state_key=state_key, persona=persona, day=day)
            return
        with model_context("life_matter", f"为生活事项 {snapshot['id']} 安排世界内的小步", session_id):
            raw = await self._ask_judge(session_id,
                    "你为角色安排一个能持续数天的个人项目，只输出JSON。不要把计划写成已经发生的事实。"
                    "基础人设的喜恶、价值观和能力优先；经历只用于温和延伸，不得改变身份、性格或强迫培养新爱好。"
                    "生活只在角色世界内模拟，不调用工具、电脑或外部服务。"
                    "阅读等活动优先匹配候选动作与地点；编程、创作、整理收藏等可用action=life_simulation，"
                    "text描述她在自己世界里做的具体小步，按在线时间投入后形成模拟经历。"
                    "不要因没有真实电脑、文件、素材或工具而阻塞个人项目；不能声称操作了现实服务器或生成了真实文件。"
                    "对现实用户的发送、购买等约定只能模拟准备，兑现仍需聊天中的真实依据。"
                    "若已有stage_notes，下一阶段必须接着其中的具体内容和next_focus推进："
                    "延续已有草稿、结论或未解问题，不重新从头概述同一个目标；不把模拟笔记说成真实资料验证。"
                    "有可做部分先安排可做部分，blocked只描述剩余阻碍，不能抹掉可做步骤。"
                    '格式：{"motivation":"一句动机","steps":[{"text":"具体下一步","action":"候选ID",'
                    '"node":"候选地点","minutes":20,"day_offset":0,"focus":"具体从哪里下手",'
                    '"open_question":"这一步准备弄清的问题"}],"blocked":""}。最多6步，每步5到60分钟。'
                    "focus和open_question是待做的方向，不是已经想通的结论。已有bookmark时接着其next_focus与未解点，"
                    "不要重新从头开始，也不要把中途想法当成阶段完成。"
                    "day_offset是从今天起经过几天才适合做，可跨多日，不要为填满日程而强行赶完。",
                    json.dumps({"persona": persona,
                                "matter": snapshot, "available": candidates}, ensure_ascii=False))
        payload = extract_json_object(raw or "") or {}
        if not await self._matter_context_current(session_id, world, state_key, persona, day=day):
            return
        async with self.session_state(session_id) as state:
            item = matters.find(state, snapshot["id"])
            if (not await self._matter_context_current(session_id, world, state_key, persona, day=day) or item is None
                    or item["revision"] != snapshot["revision"] or item["status"] in matters.CLOSED | {"paused"}):
                return  # Human edit, interruption, or newer result wins.
            allowed = {(r["action"], r["node"]) for r in self._matter_actions(state)}
            steps = []
            proposed = payload.get("steps") if isinstance(payload.get("steps"), list) else []
            for index, step in enumerate(proposed[:6]):
                if not isinstance(step, dict) or not step.get("text"):
                    continue
                if (step.get("action"), step.get("node", state.node_id)) not in allowed:
                    continue
                try:
                    minutes = min(60, max(5, int(step.get("minutes") or 20)))
                    day_offset = min(30, max(0, int(step.get("day_offset", index))))
                except (ValueError, TypeError, OverflowError):
                    continue
                definition = self.world.action_map().get(step["action"])
                if definition and definition.duration_mode == "llm":
                    minutes = min(max(minutes, max(1, definition.duration_min / 60)), max(1, definition.duration_max / 60))
                steps.append({"id": f"{item['id']}_{item['revision']}_{index}", "text": str(step["text"])[:300],
                              "goal": item["text"],
                              "focus": str(step.get("focus") or step["text"])[:300],
                              "open_question": str(step.get("open_question") or "")[:240],
                              "action": step["action"], "node": step.get("node", state.node_id), "minutes": minutes,
                              "not_before": now + day_offset * 86400, "status": "pending"})
            if steps:
                steps = [s for s in item.get("steps", []) if s.get("status") == "done"][-60:] + steps
                item.update(steps=steps, progress=min(99, round(100 * sum(s["status"] == "done" for s in steps) / len(steps))),
                            next_step=next(s["text"] for s in steps if s["status"] != "done"), status="active",
                            motivation=str(payload.get("motivation") or item.get("motivation") or "")[:300], obstacle="", blocker="",
                            needs_replan=False)
                matters.history(item, "planned", "安排了可执行的小步，尚未完成", now)
                if self._matter_can_work(state):
                    await self._start_matter_step(state, item)
            else:
                failed_at = self._now()
                item.update(status="waiting", blocker="transient", resume_at=failed_at + 900,
                            last_attempt_at=failed_at,
                            obstacle=str(payload.get("blocked") or "暂时没有得到合适的安排，稍后再想")[:300])
                matters.history(item, "blocked", item["obstacle"], now)
            matters.sync(state)

    async def _matter_context_current(self, session_id, world, state_key, persona, *, day: str = "") -> bool:
        current_persona = await self._persona_text(session_id)
        return (self.world is world and self.state_key(session_id) == state_key
                and self.is_enabled(session_id) and self.world.limits.life_matter_enabled
                and current_persona == persona and (not day or self._today_key(self._now()) == day))

    async def _review_matter_stage(self, session_id: str, snapshot: dict, *, world, state_key, persona, day: str = "") -> None:
        day = day or self._today_key(self._now())
        with model_context("life_matter", f"核对生活事项 {snapshot['id']} 的实际阶段结果", session_id):
            raw = await self._ask_judge(session_id,
                "根据事项目标和实际动作收据，判断整个目标是否已达成，只输出JSON。"
                '格式：{"outcome":"completed|continue|blocked","reason":"",'
                '"stage":{"summary":"这一阶段具体留下什么","content":"具体草稿、推演、结论或未解问题",'
                '"next_focus":"下阶段从哪一处继续","basis_step_ids":["本阶段已完成步骤ID"]}}。'
                "个人项目属于角色世界内的生活，life_simulation步骤是AI模拟，不要求真实电脑、文件或工具。"
                "只根据已经投入时间的步骤核对进展，不把未做的计划算作完成。"
                "reason可概括她在世界内完成了什么，但不得声称操作了现实服务器、通过真实测试或产生可下载文件。"
                "普通阅读只能证明阅读；模拟步骤完成后可以确认相应的世界内目标。"
                "stage只对应已完成且有投入收据的新步骤，不能重复旧stage_notes引用过的步骤。"
                "content要留下下一阶段真正能用的内容，例如短小草稿、设计取舍或具体未解问题，"
                "不是再说一遍已经进行了活动。不捏造真实书籍原文、检索结论或现实验证。"
                "已有bookmark中的未解点不能因计时结束自动消失；仍无依据就保留未确认。"
                "summary必须与content的限定一致，正文仍在核对时，标题写核对进展或仍待核实，不能写已确认。"
                "原有人设和能力优先，不从这次活动创造新身份、喜好或高超技能。"
                "现实用户的约定仍需兑现依据。可以继续则continue，暂时无法继续则blocked。",
                json.dumps({"persona": persona, "matter": snapshot}, ensure_ascii=False))
        result = extract_json_object(raw or "") or {}
        if not await self._matter_context_current(session_id, world, state_key, persona, day=day):
            return
        async with self.session_state(session_id) as state:
            item = matters.find(state, snapshot["id"])
            if (not item or item["revision"] != snapshot["revision"] or item["status"] in matters.CLOSED | {"paused"}
                    or not await self._matter_context_current(session_id, world, state_key, persona, day=day)):
                return
            reason = str(result.get("reason") or "尚未取得可核对的目标结果")[:300]
            outcome = result.get("outcome")
            if outcome not in {"completed", "continue", "blocked"}:
                return  # Failed/malformed review is not a new life result.
            stage = matters.record_stage(item, result.get("stage"), now=self._now())
            if result.get("stage") and not stage:
                self.decision_trace(session_id, "life_stage", "rejected", "阶段笔记未引用有效的新完成步骤，保留原进度等待复核", matter_id=item["id"])
                return
            if stage:
                matters.history(item, "stage_note", stage["summary"], self._now(), basis_step_ids=stage["basis_step_ids"])
                state.life_artifacts[item["id"]] = {"label": item["text"], "result": stage["summary"],
                    "content": stage["content"], "at": self._now(), "scope": "simulation", "session": item["session"]}
                state.life_artifacts = dict(list(state.life_artifacts.items())[-40:])
                self.memory.remember(session_id=item["session"], persona_id="", node_id=state.node_id,
                    content=f"（角色世界内的阶段笔记，AI模拟）{item['text']}：{stage['summary']}\n{stage['content']}",
                    memory_type="scene", source="life_matter", context=json.dumps({**stage,
                        "matter_id": item["id"], "persona_key": item.get("persona_key", "")}, ensure_ascii=False),
                    participants=[item["who"]] if item.get("who") else [])
            if outcome == "completed" and item["kind"] != "promise":
                receipts = [s.get("result", {}) for s in item["steps"] if s.get("status") == "done"]
                item.update(status="completed", progress=100, needs_review=False, next_step="", completed_at=self._now(),
                            result=stage["summary"] if stage else "；".join(str(r.get("result") or "") for r in receipts)[-500:])
                matters.history(item, "completed", "依据世界内活动确认完成；" + reason, self._now())
            elif outcome == "continue":
                item.update(status="active", needs_review=False, needs_replan=True, next_step=(stage or {}).get("next_focus") or reason, last_attempt_at=0)
                matters.history(item, "continue_stage", "进入下一阶段：" + reason, self._now())
            else:
                item.update(status="waiting", obstacle=reason, resume_at=self._now() + 3600,
                            needs_review=False, needs_replan=True)
                matters.history(item, "review_blocked", reason, self._now())
            matters.sync(state)

    async def _start_matter_step(self, state, item: dict) -> None:
        step = next((s for s in item.get("steps", []) if s.get("status") != "done"), None)
        if step is None:
            return
        if float(step.get("not_before") or 0) > self._now():
            item.update(status="waiting", resume_at=step["not_before"], next_step=step["text"])
            return
        if (step["action"], step["node"]) not in {(r["action"], r["node"]) for r in self._matter_actions(state)}:
            item.update(status="blocked", obstacle="所需动作或地点现在不可用，保留已有进展", needs_replan=True)
            matters.history(item, "blocked", item["obstacle"], self._now())
            return
        minutes = min(max(0, step["minutes"] - step.get("worked_minutes", 0)), self.world.limits.life_matter_daily_minutes - item.get("worked_minutes", 0))
        if minutes <= 0:
            return
        steps = []
        # The shared world's plan/events are visible in other conversations too.
        # Keep a private promise's wording only in its origin-scoped matter block.
        public_intent = step["text"] if not item.get("who") else "继续一件已记下的生活事项"
        if state.node_id != step["node"]:
            steps.append({"action": "walk_to", "target_node": step["node"], "kind": "quiet"})
        steps.append({"action": step["action"], "duration": minutes * 60, "kind": "quiet", "intent": public_intent})
        plan = create_plan(steps=steps, world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                           valid_for=max(7200, minutes * 60 + 1800), reason=public_intent, source="life_matter")
        plan.update(matter_id=item["id"], matter_step_id=step["id"])
        item.update(status="working", next_step=step["text"])
        matters.history(item, "started", step["text"], self._now(), step_id=step["id"])
        outcome = TickOutcome(session_id=state.session_id)
        await self._apply_plan(state, self.node(state.node_id), outcome, plan)
        await self._tick_plan(state, self.node(state.node_id), outcome, depth=0)

    def settle_life_practice(self, state, action: dict, *, at: float | None = None) -> None:
        """Record actual online effort once, even if the action never reaches completion."""
        if action.get("type") == LIFE_SIMULATION_ACTION.id:
            return  # A generic simulation cannot establish a specific interest.
        definition = self.world.action_map().get(str(action.get("type") or ""))
        elapsed = max(0, int(action.get("elapsed_ticks") or 0))
        accounted = max(0, int(action.get("practice_accounted_ticks") or 0))
        if (not definition or definition.id == "walk_to" or definition.category != "continuous" or definition.llm_level != "template"
                or definition.target_type != "none" or life_role(definition) not in {"interest", "explore"}
                or definition.tool_list() or definition.trigger_command or definition.on_complete.trigger not in {"none", "llm_followup"}
                or elapsed <= accounted or elapsed * self.tick_seconds < 60):
            return
        end = float(self._now() if at is None else at)
        start = end - (elapsed - accounted) * self.tick_seconds
        interest = state.life_interests.setdefault(definition.id, {"label": definition.name, "sessions": 0, "minutes": 0, "days": []})
        if not action.get("practice_id"):
            action["practice_id"] = uuid4().hex
            interest["sessions"] = int(interest.get("sessions", 0)) + 1
        interest.update(label=definition.name, minutes=float(interest.get("minutes", 0)) + (end - start) / 60, last_at=end)
        episodes = interest.setdefault("episodes", [])
        days = set(interest.get("days", []))
        node_id = str(action.get("node_id") or state.node_id)
        while start < end:
            local = self.local_at(start)
            boundary_hour = next((hour for hour in (5, 11, 18, 23, 24) if hour > local.hour), 24)
            boundary = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=boundary_hour)).timestamp()
            stop = min(end, boundary)
            day = self._today_key(start)
            period = "早晨" if 5 <= local.hour < 11 else "白天" if 11 <= local.hour < 18 else "晚间" if 18 <= local.hour < 23 else "夜间"
            episode = next((row for row in reversed(episodes) if row.get("practice_id") == action["practice_id"]
                            and row.get("day") == day and row.get("period") == period and row.get("node") == node_id), None)
            if episode is None:
                episode = {"practice_id": action["practice_id"], "day": day, "period": period,
                           "node": node_id, "action": definition.id, "minutes": 0}
                episodes.append(episode)
            episode.update(at=stop, minutes=episode["minutes"] + (stop - start) / 60)
            days.add(day)
            start = stop
        interest["days"], interest["episodes"] = sorted(days)[-30:], episodes[-30:]
        action["practice_accounted_ticks"] = elapsed
        state.life_interests = dict(sorted(state.life_interests.items(), key=lambda pair: pair[1].get("last_at", 0))[-40:])

    def _record_matter_action(self, state, action: dict) -> None:
        self.settle_life_practice(state, action)
        definition = self.world.action_map().get(str(action.get("type") or ""))
        minutes = max(0, int(action.get("elapsed_ticks") or 0) * self.tick_seconds / 60)
        matter_id, step_id = action.get("matter_id"), action.get("matter_step_id")
        if not matter_id or action.get("type") == "walk_to":
            return
        item = matters.find(state, matter_id)
        if item is None or item.get("status") in matters.CLOSED | {"paused"}:
            return
        if definition is None or definition.llm_level != "template":
            return  # External success must come from an explicit result, never elapsed time.
        self.settle_matter_work(state, action)
        step = next((s for s in item.get("steps", []) if s["id"] == step_id), None)
        if step is None:
            return
        simulated = definition.id == LIFE_SIMULATION_ACTION.id
        description = step["text"] if simulated else definition.name
        receipt = {"ok": True, "kind": "life_simulation" if simulated else "in_world_action", "action": definition.id,
                   "matter_id": item["id"], "step_id": step_id, "goal": item["text"], "persona_key": item.get("persona_key", ""),
                   "at": self._now(), "minutes": step.get("worked_minutes", 0), "attempt_minutes": minutes,
                   "result": f"{'世界内模拟' if simulated else '世界内进行了'}「{description}」，累计投入 {step.get('worked_minutes', 0):g} 分钟"}
        if step["worked_minutes"] + .01 < step["minutes"]:
            item["status"] = "active"
            matters.history(item, "progress", f"这一小步已投入 {step['worked_minutes']:g} 分钟，稍后继续", self._now(), receipt=receipt)
            return
        if matters.complete_step(state, matter_id, step_id, now=self._now(), receipt=receipt):
            self.memory.remember(session_id=item["session"], persona_id="", node_id=state.node_id,
                content=f"（{'生活模拟，仅属于角色世界' if simulated else '生活事项的世界内进展'}）{item['text']}：{receipt['result']}", memory_type="scene",
                source="life_matter", context=json.dumps(receipt, ensure_ascii=False),
                participants=[item["who"]] if item.get("who") else [])
