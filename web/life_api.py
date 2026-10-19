"""Life board and evidence endpoints, protected by the same editor guard and scope."""
from __future__ import annotations

import copy
import math

from astrbot.api.web import error_response, json_response, request

from ..core.life_matters import CLOSED, ensure, visible


class LifeApiMixin:
    async def api_social_learning(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session = self._scope(request.query.get("session", ""))
        if not session:
            return error_response("请先选择会话")
        user = str(request.query.get("user_id") or "")
        if user and self.engine.profiles.profile(session, user) is None:
            return error_response("当前会话没有这个联系人")
        return json_response({"session": session, "vocabulary": await self.db.call("vocabulary", session),
            "interaction": await self.db.call("interaction_detail", session, user) if user else None})

    async def api_social_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session = self._scope(payload.get("session"))
        if not session or not self.engine.is_enabled(session):
            return error_response("请先选择已启用的会话")
        operation = str(payload.get("operation") or "")
        if operation == "vocabulary":
            ok = await self.db.call("set_vocabulary", session, str(payload.get("term") or ""),
                str(payload.get("description") or ""), manual=True, status=str(payload.get("status") or "active"))
        elif operation in {"habits", "resume"}:
            user = str(payload.get("user_id") or "")
            if not user or self.engine.profiles.profile(session, user) is None:
                return error_response("当前会话没有这个联系人")
            detail = await self.db.call("interaction_detail", session, user)
            habits = [{"text": str(text).strip()[:80], "source_ids": [], "manual": True}
                      for text in str(payload.get("text") or "").splitlines() if str(text).strip()][:3]
            if operation == "resume":
                habits = detail["habits"]
            try:
                revision = int(payload.get("revision", -1))
            except (ValueError, TypeError):
                return error_response("请刷新后再修改")
            if operation == "resume":
                ok = await self.db.call("resume_interaction_style", session, user, revision)
            else:
                ok = await self.db.call("save_interaction_style", session, user, habits,
                    revision=revision, through=detail["cursor"], manual=True)
        else:
            return error_response("未知的操作")
        return json_response({"ok": True}) if ok else error_response("内容无效、已达到容量，或资料已变化；请刷新后重试")

    async def api_decision_traces(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session = self._scope(request.query.get("session", ""))
        if not session:
            return error_response("请先选择会话")
        return json_response({"session": session, "items": await self.db.call("decision_traces", session,
            trace=str(request.query.get("trace") or "")[:100])})

    async def api_life(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("请先选择会话")
        with self.engine.config_scope(session_id):
            async with self.engine.session_state(session_id) as state:
                items = [item for item in ensure(state, self.engine._now()) if visible(item, session_id)]
                result = copy.deepcopy({"session": session_id, "items": items,
                    "activity": self.engine.activity_view(state), "reason": (state.current_plan or {}).get("reason", ""),
                    "choice": self.engine.life_choice(state),
                    "recovery": state.life_recovery, "interests": state.life_interests,
                    "artifacts": [item for item in state.life_artifacts.values() if item.get("session") == session_id],
                    "semantic": self.engine.memory.semantic.status,
                    "active": sum(item.get("status") not in CLOSED for item in items)})
        result["budget"] = await self.db.call("kv_get", "background_budget:" + self.engine._today_key(self.engine._now()), {})
        return json_response(result)

    async def api_life_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id or not self.engine.is_enabled(session_id):
            return error_response("请先选择已启用的会话")
        try:
            with self.engine.config_scope(session_id):
                async with self.engine.session_state(session_id) as state:
                    operation = str(payload.get("operation") or "")
                    if operation == "create":
                        item = self.engine.add_life_matter(state, str(payload.get("text") or ""),
                            session=session_id, motivation=str(payload.get("motivation") or ""), kind=str(payload.get("kind") or "project"))
                        if not item:
                            raise ValueError("请填写内容；活跃事项满额时请先暂停或结束已有事项")
                    else:
                        item = next((row for row in ensure(state, self.engine._now()) if row["id"] == str(payload.get("id") or "")), None)
                        if item is None or not visible(item, session_id):
                            raise ValueError("当前会话内找不到这件事项")
                        resume_at = float(payload.get("resume_at") or 0)
                        if operation == "postpone" and (not math.isfinite(resume_at) or resume_at <= self.engine._now()):
                            raise ValueError("请选择将来的时间")
                        item = self.engine.manage_life_matter(state, str(payload.get("id") or ""), operation,
                            reason=str(payload.get("reason") or ""), resume_at=resume_at,
                            **{key: payload[key] for key in ("text", "motivation", "next_step") if key in payload})
                    return json_response({"ok": True, "item": item})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))

    async def api_memory_evidence(self):
        guard = self._guard()
        if guard is not None:
            return guard
        try:
            memory_id = int(request.query.get("id") or 0)
        except (TypeError, ValueError):
            return error_response("记忆ID无效")
        # Check the selected editor scope before revealing source snapshots.
        sessions = self._memory_sessions(request.query.get("session", ""))
        row = await self.db.call("memory_by_id", memory_id)
        if not row or (sessions and row["session_id"] not in sessions):
            return error_response("当前范围内找不到这条记忆")
        return json_response(await self.db.call("memory_sources", memory_id))
