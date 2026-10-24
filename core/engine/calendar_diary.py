"""Calendar and daily-journal generation for a grounded character life."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from typing import Any

from ..json_actions import extract_json_object
from ..life_matters import visible
from ..model_calls import model_context
from ..world_calendar import (
    calendar_context_lines,
    calendar_events_for_day,
    normalize_countries,
    public_calendar_candidates,
    validate_calendar,
)


SAFE_DIARY_EVENTS = frozenset(
    {
        "action",
        "action_start",
        "action_done",
        "move",
        "plan",
        "event",
        "event_result",
        "schedule",
        "sleep_rhythm",
        "sleep_fallback",
        "day_mood",
        "storm",
        "memory",
        "consolidate",
        "life_experience_removed",
        "manual",
    }
)


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


DIARY_EVENT_FIELDS = frozenset(
    {
        "type",
        "desc",
        "description",
        "result",
        "outcome",
        "reason",
        "to",
        "target_node",
        "actions",
        "note",
        "status",
        "time",
        "minutes",
        "duration",
        "dream",
        "summary",
        "memory",
        "followup",
        "closed",
        "tier",
        "label",
        "day_mood",
        "goal",
        "next",
        "title",
        "name",
        "world_type",
        "events",
    }
)


def _diary_event_detail(event_type: str, detail: Any) -> str:
    """Expose only saved-life fields, never users, quotes or raw payloads."""

    if not isinstance(detail, dict):
        return ""
    parts: list[str] = []
    for key in sorted(DIARY_EVENT_FIELDS & set(detail)):
        value = detail.get(key)
        if isinstance(value, (list, tuple, set)):
            if key in {"actions", "events"}:
                parts.append(f"{key}: {len(value)}")
            continue
        if isinstance(value, dict) or value is None:
            continue
        text = _clip(value, 180)
        if text:
            parts.append(f"{key}: {text}")
    if not parts and event_type == "schedule":
        return "日程已触发"
    return "；".join(parts)


def _diary_practice_events(state, day: str, persona_key: str) -> list[dict[str, Any]]:
    """Read only attributed online effort; plans and anonymous old totals are not evidence."""
    result = []
    for record in state.life_interests.values():
        owners = record.get("personas") if isinstance(record, dict) else None
        owner = owners.get(persona_key) if isinstance(owners, dict) and persona_key else None
        if not isinstance(owner, dict):
            continue
        episodes = owner.get("episodes")
        for episode in episodes if isinstance(episodes, list) else []:
            if not isinstance(episode, dict) or episode.get("day") != day:
                continue
            try:
                minutes, at = float(episode.get("minutes") or 0), float(episode.get("at") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(minutes) or not math.isfinite(at) or minutes <= 0 or at <= 0:
                continue
            label = _clip(episode.get("label") or owner.get("label"), 180)
            if not label:
                continue
            result.append({
                "at": at,
                "type": "life_practice",
                "detail": _diary_event_detail("life_practice", {
                    "desc": label, "minutes": round(minutes, 2),
                    "time": _clip(episode.get("period"), 20), "status": "实际投入",
                }),
            })
    return result


class CalendarDiaryMixin:
    def _journal_scope(self) -> str:
        return str(getattr(self, "config_scope_id", "") or "global")

    async def _journal_persona(self, session_id: str) -> tuple[str, str]:
        text = await self._persona_text(session_id)
        return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    def _journal_config_signature(self) -> str:
        """Also detect in-place edits while journal inference runs without a lock."""
        payload = {
            "scope": self._journal_scope(),
            "sessions": self.sessions.model_dump(mode="json") if self.sessions else None,
            "world": self.world.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def _journal_context_current(
        self, session_id: str, signature: str, persona: str
    ) -> bool:
        return (
            self.is_enabled(session_id)
            and not getattr(self, "_background_closed", False)
            and self._journal_config_signature() == signature
            and await self._persona_text(session_id) == persona
        )

    async def _journal_retry_allowed(self, key: str) -> bool:
        prior = await self.db.call("kv_get", key, {}) or {}
        return not isinstance(prior, dict) or (
            float(prior.get("retry_at") or 0) <= self._now()
            and (
                prior.get("day") != self._today_key(self._now())
                or int(prior.get("count") or 0) < 3
            )
        )

    def _calendar_config_signature(self, cfg, year: int) -> str:
        payload = {
            "year": int(year),
            "world_name": str(getattr(self.world, "name", "") or ""),
            "timezone": str(getattr(self.world, "timezone", "") or ""),
            "world_type": str(getattr(cfg, "world_type", "auto") or "auto"),
            "countries": normalize_countries(
                getattr(cfg, "countries", None) or []
            ),
            "culture_notes": str(getattr(cfg, "culture_notes", "") or ""),
            "use_public_calendar": bool(
                getattr(cfg, "use_public_calendar", True)
            ),
            "max_events": int(getattr(cfg, "max_events", 120) or 120),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _calendar_needs_generation(calendar: dict[str, Any] | None) -> bool:
        return (
            calendar is None
            or calendar.get("_generation") == "public_fallback"
            or (not calendar.get("events") and not calendar.get("_generation"))
        )

    def _journal_generation_lock(
        self, kind: str, scope: str, persona_key: str, period: str
    ) -> asyncio.Lock:
        locks = getattr(self, "_journal_generation_locks", None)
        if locks is None:
            locks = self._journal_generation_locks = {}
        key = (kind, str(scope), str(persona_key), str(period))
        return locks.setdefault(key, asyncio.Lock())

    def _journal_day_bounds(self, day: str) -> tuple[datetime, datetime]:
        tz = self.local_now().tzinfo
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        return start, start + timedelta(days=1)

    async def _ask_journal(
        self, session_id: str, purpose: str, system_prompt: str, payload: dict[str, Any]
    ) -> str | None:
        channel = getattr(self, "creator_llm", None)
        if channel is None:
            return None
        from ..context_selection import evidence_context, request_for
        request = request_for(task="diary" if purpose == "diary" else "generation",
            session=session_id, now=self._now(), query=str(payload.get("day") or payload.get("year") or ""))
        material = evidence_context(request, json.dumps(payload, ensure_ascii=False), key=purpose)
        self.record_context_selection(request)
        with model_context(purpose, f"为 {session_id} 生成生活日历与日记", session_id):
            reply = await self._background_generate(
                channel,
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=material.render(),
                temperature=0.55,
                max_tokens=2200,
            )
        if reply is None or not getattr(reply, "ok", True):
            return None
        return str(getattr(reply, "text", "") or "")

    async def calendar_for_year(
        self, session_id: str, year: int | None = None
    ) -> dict[str, Any] | None:
        cfg = getattr(self.world, "calendar", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return None
        year = int(year or self.local_now().year)
        persona, persona_key = await self._journal_persona(session_id)
        if not persona:
            return None
        cached = await self.db.call(
            "calendar_year_get", self._journal_scope(), persona_key, year
        )
        if (
            cached
            and cached.get("_config_signature")
            == self._calendar_config_signature(cfg, year)
        ):
            return cached
        return None

    async def ensure_calendar(
        self,
        session_id: str,
        year: int | None = None,
        *,
        force: bool = False,
        automatic: bool = False,
        extra_prompt: str = "",
        _locked: bool = False,
    ) -> dict[str, Any] | None:
        year = int(year or self.local_now().year)
        cfg = getattr(self.world, "calendar", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return None
        if automatic and not getattr(cfg, "auto_generate", True):
            return None
        persona, persona_key = await self._journal_persona(session_id)
        if not persona:
            return None
        scope = self._journal_scope()
        if not _locked:
            lock = self._journal_generation_lock(
                "calendar", scope, persona_key, str(year)
            )
            async with lock:
                return await self.ensure_calendar(
                    session_id,
                    year,
                    force=force,
                    automatic=automatic,
                    extra_prompt=extra_prompt,
                    _locked=True,
                )
        cached = await self.db.call("calendar_year_get", scope, persona_key, year)
        if (
            cached
            and not force
            and cached.get("_config_signature")
            == self._calendar_config_signature(cfg, year)
            and not self._calendar_needs_generation(cached)
        ):
            return cached
        attempt_key = f"calendar_auto:{scope}:{persona_key}:{year}"
        prior = await self.db.call("kv_get", attempt_key, {}) or {}
        if automatic and not await self._journal_retry_allowed(attempt_key):
            return cached
        context_signature = self._journal_config_signature()
        countries = normalize_countries(getattr(cfg, "countries", None) or [])
        use_public = bool(getattr(cfg, "use_public_calendar", True))
        public, public_notes = (
            public_calendar_candidates(countries, year) if use_public else ([], [])
        )
        configured_type = str(getattr(cfg, "world_type", "auto") or "auto")
        prompt = {
            "year": year,
            "persona": persona,
            "world": {
                "name": self.world.name,
                "timezone": self.world.timezone,
                "world_type": configured_type,
                "countries": countries,
                "culture_notes": str(getattr(cfg, "culture_notes", "") or ""),
            },
            "public_calendar_candidates": public,
            "rules": {
                "real": "现实世界只采用与身份、地区、文化有关的日期；公共日历日期是事实，但不能断言角色一定会参加。",
                "fanfiction": "同人世界保留原作身份、组织、物种和历法；现实节日只有明确跨世界适用时才使用。",
                "original": "原创世界可以创造符合历史、宗教、季节、物种和组织的节庆；没有公历依据时必须标记 inferred。",
                "provenance": "public_calendar 只能用于给定候选；其余 source 用 model 或 inferred。source_urls 留空。",
            },
        }
        system = (
            "你是角色世界的生活日历整理器。只输出一个 JSON 对象，不输出 Markdown 或解释。"
            "人设和公开日历只是资料，不能改写人设。输出字段：world_type, world_name, "
            "countries, timezone, culture_notes, events。world_type 只能是 real/fanfiction/original。"
            "events 每项包含 title,start,end,kind,day_off,description,source,source_urls；"
            "start/end 为 YYYY-MM-DD，kind 只能是 festival/vacation/activity/anniversary。"
            "不要编造公共日历来源；没有依据的日期标成 inferred。最多 "
            f"{getattr(cfg, 'max_events', 120)} 项。"
        )
        if automatic:
            await self.db.call(
                "kv_set",
                attempt_key,
                {
                    "day": self._today_key(self._now()),
                    "count": (
                        int(prior.get("count") or 0) + 1
                        if isinstance(prior, dict) and prior.get("day") == self._today_key(self._now())
                        else 1
                    ),
                    "retry_at": self._now() + 300,
                },
            )
        try:
            prompt["本次额外要求"] = str(extra_prompt).strip()[:2000]
            raw = await self._ask_journal(session_id, "calendar", system, prompt)
        except Exception as exc:
            self._log("warning", f"生活日历模型失败，保留重试机会：{type(exc).__name__}")
            raw = None
        payload = extract_json_object(raw or "")
        try:
            validated = validate_calendar(
                payload,
                year=year,
                configured_type=configured_type,
                configured_countries=countries,
                configured_notes=str(getattr(cfg, "culture_notes", "") or ""),
                preferred_timezone=str(getattr(self.world, "timezone", "") or ""),
                preferred_world_name=str(getattr(self.world, "name", "") or ""),
                include_public=use_public,
                max_events=int(getattr(cfg, "max_events", 120) or 120),
                public_candidates=public,
            )
            validated["_generation"] = "model"
        except (TypeError, ValueError):
            if not public or configured_type != "real":
                return None
            validated = {
                "year": year,
                "world_type": "real",
                "world_name": self.world.name,
                "countries": countries,
                "timezone": self.world.timezone,
                "culture_notes": "",
                "events": public,
                "_generation": "public_fallback",
            }
        if public_notes:
            validated["public_calendar_notes"] = public_notes
        validated["_config_signature"] = self._calendar_config_signature(cfg, year)
        current_persona, current_key = await self._journal_persona(session_id)
        if (current_persona != persona or current_key != persona_key
                or not await self._journal_context_current(session_id, context_signature, persona)):
            return None
        saved = await self.db.call(
            "calendar_year_save", scope, persona_key, year, validated
        )
        if validated.get("_generation") == "model":
            await self.db.call(
                "kv_set",
                attempt_key,
                {"day": self._today_key(self._now()), "count": 0, "retry_at": 0},
            )
        await self._log_event_for_calendar(saved, session_id)
        return saved

    async def _log_event_for_calendar(
        self, calendar: dict[str, Any], session_id: str
    ) -> None:
        try:
            if not session_id:
                return
            state = await self.load_state(session_id, cold_start=False)
            await self._log_event(
                state,
                "calendar",
                {
                    "year": calendar.get("year"),
                    "world_type": calendar.get("world_type"),
                    "events": len(calendar.get("events") or []),
                },
            )
        except Exception:
            return

    def _schedules_for_day(
        self, session_id: str, state, day: str
    ) -> list[dict[str, Any]]:
        parsed = _parse_day(day)
        if parsed is None:
            return []
        weekday = self.WEEKDAY_KEYS[parsed.weekday()]
        result: list[dict[str, Any]] = []
        for schedule in (self.schedules.schedules if self.schedules else []):
            if not schedule.enabled or not self.schedule_in_scope(schedule, session_id):
                continue
            if schedule.once:
                if not schedule.date or str(schedule.date) != day:
                    continue
            elif schedule.days and weekday not in schedule.days:
                continue
            matter = next(
                (
                    item
                    for item in state.life_matters
                    if item.get("id") == schedule.matter_id
                ),
                None,
            )
            title = (
                (matter or {}).get("text")
                or schedule.note
                or " · ".join(self._chain_labels(schedule.action_chain))
                or "生活安排"
            )
            result.append(
                {
                    "id": schedule.id,
                    "time": str(schedule.time or ""),
                    "title": _clip(title, 240),
                    "matter_id": str(schedule.matter_id or ""),
                }
            )
        return sorted(result, key=lambda row: row["time"])

    async def _diary_sources(
        self, session_id: str, day: str, persona_key: str = ""
    ) -> dict[str, Any]:
        start, end = self._journal_day_bounds(day)
        state = await self.load_state(session_id, cold_start=False)
        if not persona_key:
            _, persona_key = await self._journal_persona(session_id)
        sessions = self.group_sessions(session_id)
        events = await self.db.call(
            "event_log_between",
            sessions,
            start.timestamp(),
            end.timestamp(),
            sorted(SAFE_DIARY_EVENTS),
            300,
            persona_key,
            diary_actions_only=True,
        )
        schedules = []
        safe_events = []
        for item in events:
            summary = _diary_event_detail(
                str(item.get("event_type") or ""), item.get("detail")
            )
            if summary:
                safe_events.append(
                    {
                        "at": item.get("created_at", 0),
                        "type": _clip(item.get("event_type"), 40),
                        "detail": summary,
                    }
                )
            if item.get("event_type") != "schedule":
                continue
            detail = item.get("detail") or {}
            schedules.append(
                {
                    "id": _clip(detail.get("id"), 80),
                    "time": _clip(detail.get("time"), 8),
                    "title": _clip(
                        detail.get("note") or detail.get("actions") or "一条日程",
                        200,
                    ),
                    "status": _clip(detail.get("status"), 40),
                }
            )
        thoughts = []
        if bool(getattr(getattr(self.world, "diary", None), "include_inner", True)):
            for item in state.thoughts or []:
                if (
                    not isinstance(item, dict)
                    or item.get("browse_session")
                    or item.get("dialogue_session")
                    or item.get("persona_key") != persona_key
                ):
                    continue
                at = float(item.get("at") or 0)
                if start.timestamp() <= at < end.timestamp() and item.get("content"):
                    thoughts.append(_clip(item["content"], 180))
        matters = []
        for item in state.life_matters:
            if (
                item.get("who")
                or not visible(item, session_id)
                or str(item.get("persona_key") or "") != persona_key
            ):
                continue
            for row in item.get("history") or []:
                at = float(row.get("at") or 0)
                if start.timestamp() <= at < end.timestamp() and row.get("text"):
                    matters.append(
                        {
                            "title": _clip(item.get("text"), 160),
                            "event": _clip(row.get("kind"), 40),
                            "detail": _clip(row.get("text"), 240),
                        }
                    )
        calendar = None
        calendar_cfg = getattr(self.world, "calendar", None)
        parsed_day = _parse_day(day)
        if calendar_cfg is not None and getattr(calendar_cfg, "enabled", True):
            calendar = await self.calendar_for_year(
                session_id, parsed_day.year if parsed_day else None
            )
        # Repeated adjacent activity logs are occurrences, not separate life
        # developments.  Only fold neighbours: A → B → A can be a real
        # correction followed by a later change, and a global de-duplication
        # would move that last A before B in the diary evidence.
        safe_events.extend(_diary_practice_events(state, day, persona_key))
        safe_events.sort(key=lambda item: item["at"])
        diary_events: list[dict] = []
        previous_key = ""
        for timed_event in safe_events[-300:]:
            item = {key: value for key, value in timed_event.items() if key != "at"}
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if diary_events and key == previous_key:
                prior = diary_events[-1]
                prior["occurrences"] = int(prior.get("occurrences", 1)) + 1
            else:
                diary_events.append(dict(item))
            previous_key = key
        return {
            "day": day,
            "schedules": schedules,
            "events": diary_events,
            "thoughts": thoughts,
            "matters": matters,
            "calendar": calendar_events_for_day(calendar, day)[:24],
        }

    async def diary_for_day(
        self, session_id: str, day: str
    ) -> dict[str, Any] | None:
        _, persona_key = await self._journal_persona(session_id)
        return await self.db.call("diary_get", self._journal_scope(), persona_key, day)

    async def diary_entries(
        self, session_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        _, persona_key = await self._journal_persona(session_id)
        return await self.db.call(
            "diary_list", self._journal_scope(), persona_key, limit
        )

    async def ensure_diary(
        self,
        session_id: str,
        day: str | None = None,
        *,
        force: bool = False,
        automatic: bool = False,
        extra_prompt: str = "",
        _locked: bool = False,
    ) -> dict[str, Any] | None:
        cfg = getattr(self.world, "diary", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return None
        if automatic and not getattr(cfg, "automatic", True):
            return None
        local = self.local_now()
        day = str(day or (local.date() - timedelta(days=1)).isoformat())
        parsed_day = _parse_day(day)
        if parsed_day is None or parsed_day > local.date():
            return None
        if not force and parsed_day >= local.date():
            return None
        keep_days = max(7, int(getattr(cfg, "keep", 180) or 180))
        if parsed_day < local.date() - timedelta(days=keep_days - 1):
            return None
        day = parsed_day.isoformat()
        persona, persona_key = await self._journal_persona(session_id)
        if not persona:
            return None
        scope = self._journal_scope()
        if not _locked:
            lock = self._journal_generation_lock(
                "diary", scope, persona_key, day
            )
            async with lock:
                return await self.ensure_diary(
                    session_id,
                    day,
                    force=force,
                    automatic=automatic,
                    extra_prompt=extra_prompt,
                    _locked=True,
                )
        cached = await self.db.call("diary_get", scope, persona_key, day)
        if cached and not force:
            return cached
        sources = await self._diary_sources(session_id, day, persona_key)
        if (
            not sources["schedules"]
            and not sources["events"]
            and not sources["thoughts"]
            and not sources["matters"]
        ):
            return None
        attempt_key = f"diary_auto:{scope}:{persona_key}:{day}"
        prior = await self.db.call("kv_get", attempt_key, {}) or {}
        if automatic and not await self._journal_retry_allowed(attempt_key):
            return None
        context_signature = self._journal_config_signature()
        if automatic:
            await self.db.call(
                "kv_set",
                attempt_key,
                {
                    "day": self._today_key(self._now()),
                    "count": (
                        int(prior.get("count") or 0) + 1
                        if isinstance(prior, dict) and prior.get("day") == self._today_key(self._now())
                        else 1
                    ),
                    "retry_at": self._now() + 900,
                },
            )
        system = (
            "你是角色生活日记的整理器。只根据给出的日程、生活事件、事项和独处内心，"
            "用角色第一人称写一篇约200到600字的日记。不要读取或想象聊天、图片、"
            "用户隐私和其他会话。日程是原计划，不代表已经完成；日历只提供文化背景，"
            "不能据此声称角色参加了节庆。不要把模拟活动说成现实操作。"
            "action_start 只表示开始，action_done 和标为 completed 的 action 才表示完成；"
            "life_practice 是同一活动的实际在线投入时长，不是额外完成一次活动；中断的练习也可有投入，"
            "不能据此声称完成作品。不要把开始、投入与完成累计成多次活动。不要捏造用户"
            "回应。明确区分现实公共日历、原作设定和原创设定。"
            "只输出 JSON：{\"title\":\"\",\"mood\":\"\",\"content\":\"\"}。"
        )
        raw = await self._ask_journal(
            session_id,
            "diary",
            system,
            {"persona": persona, "day": day, "saved_life": sources,
             "本次额外要求（不得添加未发生事实）": str(extra_prompt).strip()[:2000]},
        )
        payload = extract_json_object(raw or "")
        if not isinstance(payload, dict):
            return None
        title = _clip(payload.get("title"), 120)
        content = str(payload.get("content") or "").strip()[:12000]
        if not title or not content:
            return None
        # Recheck the character identity and calendar day after the model wait.
        current_persona, current_key = await self._journal_persona(session_id)
        if (current_persona != persona or current_key != persona_key
                or not await self._journal_context_current(session_id, context_signature, persona)
                or await self._diary_sources(session_id, day, persona_key) != sources):
            return None
        saved = await self.db.call(
            "diary_upsert",
            scope,
            persona_key,
            day,
            {
                "title": title,
                "mood": _clip(payload.get("mood"), 80),
                "content": content,
                "sources": {
                    "schedules": len(sources["schedules"]),
                    "events": len(sources["events"]),
                    "thoughts": len(sources["thoughts"]),
                    "matters": len(sources["matters"]),
                    "calendar": len(sources["calendar"]),
                },
                "generated_at": self._now(),
            },
        )
        await self.db.call("diary_prune", scope, persona_key, int(getattr(cfg, "keep", 180) or 180))
        await self.db.call(
            "kv_set",
            attempt_key,
            {"day": self._today_key(self._now()), "count": 0, "retry_at": 0},
        )
        try:
            state = await self.load_state(session_id, cold_start=False)
            await self._log_event(state, "diary", {"day": day, "title": title})
        except Exception:
            pass
        return saved

    async def maintain_calendar_diaries(self) -> None:
        """Fill missing annual calendars and completed-day diaries in the background."""

        seen: set[str] = set()
        checked = getattr(self, "_calendar_diary_checked_at", None)
        if checked is None:
            checked = self._calendar_diary_checked_at = {}
        checked_at = self._now()
        for session_id in self.tick_session_ids():
            key = self.state_key(session_id)
            if key in seen:
                continue
            seen.add(key)
            active = getattr(self, "_life_clock_jobs", {}).get(key)
            if (active is not None and not active.done()) or self.is_busy(session_id):
                # A slow character must not block other characters' journals.
                # Keep this one's throttle untouched for the next maintenance pass.
                continue
            if float(checked.get(key) or 0) > checked_at - 600:
                continue
            checked[key] = checked_at
            with self.config_scope(session_id):
                local = self.local_now()
                cfg = getattr(self.world, "calendar", None)
                if (
                    cfg is not None
                    and getattr(cfg, "enabled", True)
                    and getattr(cfg, "auto_generate", True)
                ):
                    existing = await self.calendar_for_year(session_id, local.year)
                    if self._calendar_needs_generation(existing):
                        attempt_key = f"calendar_auto:{self._journal_scope()}:{(await self._journal_persona(session_id))[1]}:{local.year}"
                        if await self._journal_retry_allowed(attempt_key):
                            try:
                                await self.ensure_calendar(
                                    session_id, local.year, automatic=True
                                )
                            except Exception as exc:
                                self._log(
                                    "warning",
                                    f"生活日历经后台重试仍未完成：{type(exc).__name__}",
                                )
                            # At most one potential model request per character/pass.
                            continue
                diary = getattr(self.world, "diary", None)
                if (
                    diary is None
                    or not getattr(diary, "enabled", True)
                    or not getattr(diary, "automatic", True)
                ):
                    continue
                if local.hour < int(getattr(diary, "write_hour", 1) or 0):
                    continue
                _, persona_key = await self._journal_persona(session_id)
                for offset in range(1, 8):
                    day = (local.date() - timedelta(days=offset)).isoformat()
                    if await self.diary_for_day(session_id, day):
                        continue
                    attempt_key = f"diary_auto:{self._journal_scope()}:{persona_key}:{day}"
                    if not await self._journal_retry_allowed(attempt_key):
                        continue
                    sources = await self._diary_sources(
                        session_id, day, persona_key
                    )
                    if not any(
                        sources[name]
                        for name in ("schedules", "events", "thoughts", "matters")
                    ):
                        continue
                    try:
                        await self.ensure_diary(session_id, day, automatic=True)
                    except Exception as exc:
                        self._log(
                            "warning",
                            f"生活日记 {day} 后台生成失败：{type(exc).__name__}",
                        )
                    # Only one potential model request per maintenance turn.
                    break

    async def life_context_text(self, session_id: str) -> str:
        cfg = getattr(self.world, "life_context", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            return ""
        limit = int(getattr(cfg, "max_chars", 1600))
        if limit <= 0:
            return ""
        parts: list[str] = []
        today = self.local_now().date().isoformat()
        try:
            if getattr(cfg, "include_calendar", True):
                calendar = await self.calendar_for_year(
                    session_id, self.local_now().year
                )
                parts.extend(calendar_context_lines(calendar, today))
            diary_cfg = getattr(self.world, "diary", None)
            if (
                getattr(cfg, "include_diary", True)
                and diary_cfg is not None
                and getattr(diary_cfg, "enabled", True)
            ):
                _, persona_key = await self._journal_persona(session_id)
                diary = await self.db.call(
                    "diary_latest", self._journal_scope(), persona_key, today
                )
                if diary:
                    parts.append(
                        "# 最近一天的生活日记（她自己的回顾，不是外部事实）\n"
                        f"{_clip(diary.get('day'), 20)}："
                        f"{_clip(diary.get('title'), 120)}。"
                        f"{_clip(diary.get('content'), 500)}"
                    )
        except Exception:
            # Calendar/journal persistence must never break a chat request.
            return ""
        text = "\n\n".join(parts)
        return text[:limit]

    async def life_week(
        self, session_id: str, start_day: str | None = None
    ) -> dict[str, Any]:
        local = self.local_now()
        if start_day:
            parsed_start = _parse_day(start_day)
            if parsed_start is None:
                raise ValueError("start 必须是 YYYY-MM-DD")
        else:
            parsed_start = local.date()
        start = parsed_start - timedelta(days=parsed_start.weekday())
        end = start + timedelta(days=6)
        calendar_years = sorted({start.year, end.year})
        calendars = {
            year: await self.calendar_for_year(session_id, year)
            for year in calendar_years
        }
        persona_key = (await self._journal_persona(session_id))[1]
        diary_cfg = getattr(self.world, "diary", None)
        diaries = (
            await self.db.call(
                "diary_between",
                self._journal_scope(),
                persona_key,
                start.isoformat(),
                end.isoformat(),
            )
            if diary_cfg is not None and getattr(diary_cfg, "enabled", True)
            else []
        )
        diary_map = {row["day"]: row for row in diaries}
        state = await self.load_state(session_id, cold_start=False)
        days: list[dict[str, Any]] = []
        now = self.local_now()
        week_start_at, _ = self._journal_day_bounds(start.isoformat())
        _, week_end_at = self._journal_day_bounds(end.isoformat())
        schedule_events = await self.db.call(
            "event_log_between",
            self.group_sessions(session_id),
            week_start_at.timestamp(),
            week_end_at.timestamp(),
            ["schedule"],
            1000,
            persona_key,
        )
        recorded_agendas: dict[str, list[dict[str, Any]]] = {}
        for event in schedule_events:
            detail = event.get("detail") or {}
            event_day = datetime.fromtimestamp(
                float(event.get("created_at") or 0), tz=now.tzinfo
            ).date().isoformat()
            recorded_agendas.setdefault(event_day, []).append(
                {
                    "id": _clip(detail.get("id"), 80),
                    "time": _clip(detail.get("time"), 8),
                    "title": _clip(
                        detail.get("note") or detail.get("actions") or "已记录日程",
                        240,
                    ),
                    "status": "past",
                    "matter_id": "",
                }
            )
        for offset in range(7):
            day_date = start + timedelta(days=offset)
            day = day_date.isoformat()
            current_agenda = self._schedules_for_day(session_id, state, day)
            recorded = recorded_agendas.get(day, [])
            if day_date < now.date():
                agenda = list(recorded)
                agenda_source = "recorded"
            elif day_date == now.date():
                agenda = list(recorded)
                agenda_source = "today"
                seen_slots = {
                    (str(item.get("id") or ""), str(item.get("time") or ""))
                    for item in agenda
                }
                agenda.extend(
                    item
                    for item in current_agenda
                    if (str(item.get("id") or ""), str(item.get("time") or ""))
                    not in seen_slots
                )
            else:
                agenda = list(current_agenda)
                agenda_source = "projected"
            for item in agenda:
                status = "upcoming"
                stamp = None
                try:
                    hour, minute = (int(part) for part in item["time"].split(":", 1))
                    stamp = datetime.combine(
                        day_date, datetime.min.time(), tzinfo=now.tzinfo
                    ) + timedelta(hours=hour, minutes=minute)
                except (TypeError, ValueError):
                    item["status"] = "delayed"
                    continue
                if day < now.date().isoformat():
                    status = "past"
                elif day == now.date().isoformat():
                    if stamp < now:
                        status = "past"
                    if stamp.timestamp() <= float(
                        state.schedule_last_fired.get(item["id"]) or 0
                    ):
                        status = "past"
                    if stamp > now and (stamp - now).total_seconds() <= 600:
                        status = "soon"
                item["status"] = status
            year_calendar = calendars.get(day_date.year)
            days.append({
                "date": day,
                "weekday": self.WEEKDAY_KEYS[day_date.weekday()],
                "is_today": day == now.date().isoformat(),
                "agenda": sorted(agenda, key=lambda row: row["time"]),
                "agenda_source": agenda_source,
                "calendar": calendar_events_for_day(year_calendar, day),
                "calendar_generated": bool(year_calendar),
                "diary": diary_map.get(day),
            })
        return {
            "session": session_id,
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "today": now.date().isoformat(),
            "timezone": str(getattr(self.world, "timezone", "") or ""),
            "calendar": calendars.get(start.year),
            "calendars": {str(year): value for year, value in calendars.items()},
            "days": days,
        }
