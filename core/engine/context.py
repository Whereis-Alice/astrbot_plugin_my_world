"""One context preparation path for production, previews and isolated evaluations."""
from __future__ import annotations

from types import SimpleNamespace
from hashlib import sha256

from ..context_selection import ContextRequest, request_for, select_chat_rows


class ContextMixin:
    def context_request(self, state, target: str, *, task: str = "reply", text: str = "",
                        rows: list[dict] | None = None, focus: str = "", persona: str = "") -> ContextRequest:
        if rows is None:
            rows = self.chat_window(state, target)
        rows = self._context_origins(state, rows)
        parts = target.split(":", 2)
        if not focus and len(parts) == 3 and parts[1] in {"FriendMessage", "PrivateMessage"}:
            focus = parts[2]
        # An explicit action target is an ID; a nickname in generated text is not.
        return request_for(task=task, session=target, now=self._now(), query=text,
                           rows=rows, focus=focus, persona=persona,
                           node_id=str(getattr(state, "node_id", "") or ""))

    @staticmethod
    def _context_origins(state, rows: list[dict]) -> list[dict]:
        # Older archives belong to their owning life, never implicitly to a
        # different destination selected for this turn.
        return [{**row, "origin": str(row.get("origin") or row.get("session") or state.session_id)}
                for row in rows if isinstance(row, dict)]

    def semantic_context_items(self, state, request: ContextRequest) -> list:
        from ..context_selection import ContextItem, context_eligible, relevance
        from ..context_sources import life_items, profile_fact_items
        materials = life_items(state, request) if self.world.state_dynamics.own_topic_enabled else []
        focus = request.focus_user or (request.people[0] if request.task == "participation" and request.people else "")
        if self.profiles.enabled() and focus and focus in request.people:
            person = self.person_payload(request.session, focus)
            if person:
                facts = [item for item in person.get("facts", []) if item.get("status", "active") == "active"]
                materials += profile_fact_items(focus, facts, request.session)
        from ..action_facts import state_execution_view
        event_state = state_execution_view(state, self.world, persona_key=request.life_persona, now=request.now)
        for index, event in enumerate(event_state.recent_events or []):
            if self.prompts._trivial_event(event):
                continue
            line = self.prompts._event_line(event)
            if line:
                materials.append(ContextItem(f"experience:{index}", "experience",
                    "# 最近发生的事\n" + line,
                    at=float(event.get("at") or event.get("created_at") or 0)))
        planning_events = [item for item in materials
                           if item.kind == "experience" and item.key.startswith("experience:")]
        if request.task == "plan" and planning_events:
            materials = [item for item in materials if item not in planning_events]
            lines = [item.summary.partition("\n")[2] for item in planning_events]
            materials.append(ContextItem("experience:recent", "experience",
                "# 最近发生的事\n" + "\n".join(lines[-8:]), at=request.now, priority=.2))
        from ..visit_presence import active_visit
        inner = self.prompts._inner_voice(
            state, request.session, dialogue_only=request.task in {"reply", "participation"},
            persona_key=request.life_persona, subjective_only=bool(active_visit(self, request.session)))
        if inner:
            materials.append(ContextItem("inner:current", "thought",
                "# 当时的内心活动（主观想法，仅你可见，不直接复述）\n" + inner))
        for index, topic in enumerate(self.prompts._due_open_topics(
                state, request.session, focus_user=request.focus_user,
                persona_key=request.life_persona)):
            body = self.prompts.open_topics_layer([topic], who_name="", pronoun="她")
            if body:
                who = str(topic.get("who") or topic.get("user_id") or "")
                materials.append(ContextItem(f"topic:{index}", "topic", body,
                    people=(who,) if who else (), continuing=True,
                    session=str(topic.get("session") or state.session_id)))
        note = state.chat_note_text(
            request.session,
            max_minutes=int(getattr(self.world.context, "chat_note_max_minutes", 30)),
            now=request.now)
        if note:
            materials.append(ContextItem("chat:note", "topic", "# 刚才的话题备忘\n" + note))
        materials = [item for item in materials if context_eligible(request, item)]
        materials.sort(key=lambda item: (relevance(request.query, item.topic_text), item.continuing, item.at), reverse=True)
        return materials[:64]

    def restore_context_semantics(self, request: ContextRequest) -> None:
        config = self.world.memory
        request.semantic_context = (self.memory.semantic.cached_context(
            request.session, request.persona, [request.query, *request.people_queries.values()],
            life_persona_key=request.life_persona, node_id=request.node_id)
            if config.semantic_enabled and config.automatic_recall != "manual" and config.recall_max_chars > 0 else {})

    async def prepare_context_memory(self, request: ContextRequest, *, state=None) -> None:
        if not request.life_persona:
            persona = await self._persona_text(request.session)
            request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
        if request.task in {"sleep", "diary", "review", "generation"}:
            return
        config = self.world.memory
        if (config.semantic_enabled and config.automatic_recall != "manual" and config.recall_max_chars > 0
                and self.memory.semantic._ready()):
            from ..context_selection import context_identity
            if state is None:
                state = await self.load_state(request.session, cold_start=False)
            materials = self.semantic_context_items(state, request)
            await self.memory.semantic.prepare_many(
                request.session, request.persona,
                [request.query, *request.people_queries.values()],
                automatic=True, life_persona_key=request.life_persona,
                node_id=request.node_id, task=request.task, people=request.people,
                context_texts={context_identity(item): item.topic_text for item in materials})
        self.restore_context_semantics(request)

    def context_memories(self, state, request: ContextRequest, rows: list[dict] | None = None):
        self.restore_context_semantics(request)
        if self.world.limits.max_think_memory <= 0:
            return []
        return self.memory.recall_context(request, node_id=state.node_id, records=rows,
                                         limit=self.world.limits.max_think_memory)

    def context_profiles(self, state, request: ContextRequest) -> str:
        self.restore_context_semantics(request)
        focus = request.focus_user or (request.people[0] if request.task == "participation" and request.people else "")
        blocks = []
        if focus and focus in request.people:
            blocks.append(self.profile_block(state,
                SimpleNamespace(session_id=request.session, user_id=focus, user_name=focus),
                context_request=request))
        participants = set(request.people) - ({focus} if focus else set())
        if participants:
            others = self.other_people(state, request.session, exclude_user_id=focus)
            others = [item for item in others if str(item.get("user_id") or "") in participants]
            if others:
                blocks.append(self.prompts.profile_block(None, others,
                    limit_others=int(self.world.profile.digest_limit)))
        return "\n\n".join(block for block in blocks if block)

    def context_chat(self, state, request: ContextRequest, rows: list[dict] | None = None) -> list[dict]:
        source = self.chat_window(state, request.session) if rows is None else rows
        source = self._context_origins(state, source)
        selected = select_chat_rows(request, source,
            maximum=max(8, min(40, int(self.world.context.chat_answered_lines) + int(self.world.context.chat_lines))),
            archive=self._context_origins(state, state.recent_chat))
        if request.task not in {"plan", "reply", "expression"}:
            return selected
        # These turns can see bounded, labelled activity in sibling sessions
        # of the same character. It remains background; only the target session
        # is eligible for a direct reply.
        from ..state import take_last_chat_groups
        allowed_sessions = set(self.group_sessions(request.session))
        elsewhere = [row for row in source
                     if self._chat_origin(row, state) in allowed_sessions
                     and self._chat_origin(row, state) != request.session]
        elsewhere = take_last_chat_groups(elsewhere, max(0, int(self.world.context.chat_elsewhere_lines)))
        return sorted([*selected, *elsewhere], key=lambda row: int(row.get("seq") or 0))

    def context_emotions(self, state, request: ContextRequest) -> str:
        from ..context_selection import ContextItem, select_context
        materials = []
        sources = (("heart_knots", "about", "text", "since"), ("grudges", "user_id", "reason", "at"))
        for field, person_field, text_field, time_field in sources:
            if (field == "heart_knots" and not self.world.state_dynamics.heart_knot_enabled
                    or field == "grudges" and not self.world.profile.grudge_enabled):
                continue
            for index, row in enumerate(getattr(state, field, []) or []):
                uid = str(row.get(person_field) or "")
                origin = str(row.get("session") or state.session_id)
                if (not self._emotion_belongs(state, row, request.life_persona)
                        or origin != request.session or (uid and uid not in request.people)):
                    continue
                materials.append(ContextItem(f"{field}:{index}", "relationship",
                    f"# 当时留下的心情（仅本会话；不当成任务，也不强行翻旧账）\n涉及用户ID：{uid or '自己'}；{row.get(text_field) or ''}",
                    people=(uid,) if uid else (), session=origin,
                    at=float(row.get(time_field) or 0), expires=float(row.get("until") or 0), priority=.2))
        return select_context(request, materials, budget=600, channel="feelings").render()

    async def task_background(self, state, *, task: str, query: str, required: str = "", evidence: bool = False,
                              request: ContextRequest | None = None) -> str:
        """Specialised jobs keep their own required evidence, share only background selection."""
        from ..context_sources import life_items, memory_context_items
        from ..context_selection import CONTEXT_GUIDANCE, ContextItem, select_context
        if request is None:
            request = self.context_request(state, state.session_id, task=task, text=query, rows=[])
            await self.prepare_context_memory(request, state=state)
        request.evidence = evidence
        materials = life_items(state, request) if self.world.state_dynamics.own_topic_enabled else []
        # Sleep and diary use their bounded evidence, never a random social memory.
        if task not in {"sleep", "diary", "review", "generation"}:
            memories = [item for item in self.context_memories(state, request)
                        if not item.source_scoped and not item.participants and not item.related_users]
            materials += memory_context_items(state, request, memories,
                stamp=lambda at: self.prompts._memory_stamp(at, self.local_now()),
                life_enabled=self.world.state_dynamics.own_topic_enabled)
        if required:
            materials.append(ContextItem("task:evidence", "event", required, required=True))
        result = select_context(request, materials, channel="task")
        self.record_context_selection(request)
        return CONTEXT_GUIDANCE + "\n\n" + result.render()

    def record_context_selection(self, request: ContextRequest) -> None:
        self.decision_trace(request.session, "context", "selected", "按本轮任务、参与者与生活连续性选取背景",
                            task=request.task, selection=request.trace)
