"""Life board and evidence endpoints, protected by the same editor guard and scope."""
from __future__ import annotations

import copy
import math
from datetime import date, timedelta

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
                    **self.engine.life_overview(state, session_id),
                    "activity": self.engine.activity_view(state), "reason": (state.current_plan or {}).get("reason", ""),
                    "choice": self.engine.life_choice(state),
                    "recovery": state.life_recovery, "interests": state.life_interests,
                    "artifacts": [item for item in state.life_artifacts.values() if item.get("session") == session_id],
                    "semantic": self.engine.memory.semantic.status,
                    "active": sum(item.get("status") not in CLOSED for item in items)})
            budget_day = self.engine._today_key(self.engine._now())
        result["budget"] = await self.db.call("kv_get", "background_budget:" + budget_day, {})
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
                    if operation == "forget_experience":
                        action = str(payload.get("action") or "")
                        if action not in state.life_interests:
                            raise ValueError("找不到这条生活经验，请刷新")
                        removed = state.life_interests.pop(action)
                        state.add_event("life_experience_removed", {"action": action, "label": removed.get("label"), "source": "管理员整理"})
                        return json_response({"ok": True})
                    if operation == "create":
                        _, persona_key = await self.engine._journal_persona(
                            session_id
                        )
                        item = self.engine.add_life_matter(state, str(payload.get("text") or ""),
                            session=session_id, motivation=str(payload.get("motivation") or ""), kind=str(payload.get("kind") or "project"),
                            persona_key=persona_key)
                        if not item:
                            raise ValueError("请填写内容；活跃事项满额时请先暂停或结束已有事项")
                    else:
                        item = next((row for row in ensure(state, self.engine._now()) if row["id"] == str(payload.get("id") or "")), None)
                        if item is None or not visible(item, session_id):
                            raise ValueError("当前会话内找不到这件事项")
                        if "revision" in payload and int(payload["revision"]) != int(item.get("revision", 0)):
                            raise ValueError("事项已有新进展，请刷新后再调整")
                        if operation == "appointment":
                            day = str(payload.get("date") or "")
                            from datetime import date
                            if not day or date.fromisoformat(day) < self.engine.local_now().date():
                                raise ValueError("请选择今天或将来的日期")
                            ok, note = await self.engine.schedule_add({"id": "life_" + item["id"] + "_" + day.replace("-", "") + "_" + str(payload.get("time") or "").replace(":", ""),
                                "time": payload.get("time"), "date": day, "once": True, "matter_id": item["id"],
                                "note": item["text"], "sessions": [session_id]}, session_id=session_id)
                            if not ok:
                                raise ValueError(note)
                            return json_response({"ok": True, "note": note})
                        resume_at = float(payload.get("resume_at") or 0)
                        if operation == "postpone" and (not math.isfinite(resume_at) or resume_at <= self.engine._now()):
                            raise ValueError("请选择将来的时间")
                        item = self.engine.manage_life_matter(state, str(payload.get("id") or ""), operation,
                            reason=str(payload.get("reason") or ""), resume_at=resume_at,
                            **{key: payload[key] for key in ("text", "motivation", "next_step") if key in payload})
                    return json_response({"ok": True, "item": item})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))

    async def api_life_week(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("请先选择会话")
        start = str(request.query.get("start", "") or "").strip()
        if start:
            try:
                date.fromisoformat(start)
            except ValueError:
                return error_response("start 必须是 YYYY-MM-DD")
        try:
            with self.engine.config_scope(session_id):
                return json_response(
                    await self.engine.life_week(session_id, start or None)
                )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def api_calendar(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("请先选择会话")
        try:
            year = int(request.query.get("year", self.engine.local_now().year))
        except (TypeError, ValueError):
            return error_response("year 必须是有效年份")
        if not 1900 <= year <= 2200:
            return error_response("year 超出支持范围")
        with self.engine.config_scope(session_id):
            cfg = self.engine.world.calendar
            enabled = bool(getattr(cfg, "enabled", True))
            calendar = (
                await self.engine.calendar_for_year(session_id, year)
                if enabled
                else None
            )
            return json_response(
                {
                    "session": session_id,
                    "year": year,
                    "enabled": enabled,
                    "auto_generate": bool(getattr(cfg, "auto_generate", True)),
                    "world_type": str(getattr(cfg, "world_type", "auto")),
                    "countries": list(getattr(cfg, "countries", []) or []),
                    "calendar": calendar,
                }
            )

    async def api_calendar_generate(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id or not self.engine.is_enabled(session_id):
            return error_response("请先选择已启用的会话")
        try:
            year = int(payload.get("year") or self.engine.local_now().year)
        except (TypeError, ValueError):
            return error_response("year 必须是有效年份")
        if not 1900 <= year <= 2200:
            return error_response("year 超出支持范围")
        with self.engine.config_scope(session_id):
            if not getattr(self.engine.world.calendar, "enabled", True):
                return error_response("请先在设置中启用生活日历")
            calendar = await self.engine.ensure_calendar(
                session_id, year, force=True, automatic=False, extra_prompt=str(payload.get("extra_prompt") or "")
            )
        if calendar is None:
            return error_response(
                "日历没有生成：请检查内容生成模型、后台预算与人设，或稍后重试"
            )
        return json_response({"ok": True, "year": year, "calendar": calendar})

    async def api_diary(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("请先选择会话")
        day = str(request.query.get("day", "") or "").strip()
        if day:
            try:
                date.fromisoformat(day)
            except ValueError:
                return error_response("day 必须是 YYYY-MM-DD")
        try:
            limit = max(1, min(365, int(request.query.get("limit", 30) or 30)))
        except (TypeError, ValueError):
            limit = 30
        with self.engine.config_scope(session_id):
            cfg = self.engine.world.diary
            enabled = bool(getattr(cfg, "enabled", True))
            entry = (
                await self.engine.diary_for_day(session_id, day)
                if enabled and day
                else None
            )
            entries = (
                await self.engine.diary_entries(session_id, limit) if enabled else []
            )
            return json_response(
                {
                    "session": session_id,
                    "enabled": enabled,
                    "automatic": bool(getattr(cfg, "automatic", True)),
                    "day": day,
                    "entry": entry,
                    "entries": entries,
                }
            )

    async def api_diary_generate(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id or not self.engine.is_enabled(session_id):
            return error_response("请先选择已启用的会话")
        day = str(payload.get("day") or "").strip()
        if not day:
            return error_response("请选择要补写的日期")
        try:
            target = date.fromisoformat(day)
        except ValueError:
            return error_response("day 必须是 YYYY-MM-DD")
        if target >= self.engine.local_now().date():
            return error_response("只能补写已经结束的日期")
        with self.engine.config_scope(session_id):
            diary_cfg = self.engine.world.diary
            if not getattr(diary_cfg, "enabled", True):
                return error_response("请先在设置中启用每日日记")
            keep_days = max(7, int(getattr(diary_cfg, "keep", 180) or 180))
            if target < self.engine.local_now().date() - timedelta(
                days=keep_days - 1
            ):
                return error_response(
                    f"日期超出日记保留范围（最近 {keep_days} 天）"
                )
            entry = await self.engine.ensure_diary(
                session_id, day, force=True, automatic=False, extra_prompt=str(payload.get("extra_prompt") or "")
            )
        if entry is None:
            return error_response(
                "这一天没有足够的已保存生活记录，未生成凭空日记"
            )
        return json_response({"ok": True, "day": day, "entry": entry})

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
