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
        if not focus and ":PrivateMessage:" in target:
            focus = target.rsplit(":", 1)[-1]
        # An explicit action target is an ID; a nickname in generated text is not.
        return request_for(task=task, session=target, now=self._now(), query=text,
                           rows=rows, focus=focus, persona=persona)

    @staticmethod
    def _context_origins(state, rows: list[dict]) -> list[dict]:
        # Older archives belong to their owning life, never implicitly to a
        # different destination selected for this turn.
        return [{**row, "origin": str(row.get("origin") or row.get("session") or state.session_id)}
                for row in rows if isinstance(row, dict)]

    async def prepare_context_memory(self, request: ContextRequest) -> None:
        persona = await self._persona_text(request.session)
        request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
        if self.world.memory.semantic_enabled and request.task not in {"sleep", "diary", "review", "generation"}:
            await self.memory.semantic.prepare(request.session, request.persona, request.query, automatic=True)

    def context_memories(self, state, request: ContextRequest, rows: list[dict] | None = None):
        if self.world.limits.max_think_memory <= 0:
            return []
        return self.memory.recall_context(request, node_id=state.node_id, records=rows,
                                         limit=self.world.limits.max_think_memory)

    def context_profiles(self, state, request: ContextRequest) -> str:
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
            for index, row in enumerate(getattr(state, field, []) or []):
                uid = str(row.get(person_field) or "")
                origin = str(row.get("session") or state.session_id)
                if origin != request.session or (uid and uid not in request.people):
                    continue
                materials.append(ContextItem(f"{field}:{index}", "relationship",
                    f"# 当时留下的心情（仅本会话；不当成任务，也不强行翻旧账）\n涉及用户ID：{uid or '自己'}；{row.get(text_field) or ''}",
                    people=(uid,) if uid else (), session=origin,
                    at=float(row.get(time_field) or 0), expires=float(row.get("until") or 0), priority=.2))
        return select_context(request, materials, budget=600, channel="feelings").render()

    async def task_background(self, state, *, task: str, query: str, required: str = "", evidence: bool = False,
                              request: ContextRequest | None = None) -> str:
        """Specialised jobs keep their own required evidence, share only background selection."""
        from ..context_sources import life_items
        from ..context_selection import CONTEXT_GUIDANCE, ContextItem, select_context
        if request is None:
            request = self.context_request(state, state.session_id, task=task, text=query, rows=[])
            await self.prepare_context_memory(request)
        request.evidence = evidence
        materials = life_items(state, request) if self.world.state_dynamics.own_topic_enabled else []
        # Sleep and diary use their bounded evidence, never a random social memory.
        if task not in {"sleep", "diary", "review", "generation"}:
            materials.extend(ContextItem(f"memory:{item.id}", "memory",
                item.render(stamp=self.prompts._memory_stamp(item.created_at, self.local_now())),
                at=item.created_at, session=item.session_id if item.source_scoped else "",
                people=tuple(item.participants or item.related_users))
                for item in self.context_memories(state, request)
                if not item.source_scoped and not item.participants and not item.related_users)
        if required:
            materials.append(ContextItem("task:evidence", "event", required, required=True))
        result = select_context(request, materials, channel="task")
        self.record_context_selection(request)
        return CONTEXT_GUIDANCE + "\n\n" + result.render()

    def record_context_selection(self, request: ContextRequest) -> None:
        self.decision_trace(request.session, "context", "selected", "按本轮任务、参与者与生活连续性选取背景",
                            task=request.task, selection=request.trace)
