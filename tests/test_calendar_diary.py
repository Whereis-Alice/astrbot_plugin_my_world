"""Calendar provenance, diary privacy and the life-week integration."""
from __future__ import annotations

import asyncio
import json
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from core.world_calendar import (
    public_calendar_candidates,
    stable_event_id,
    validate_calendar,
)
from tests import test_scope_api as scope
from tests import test_engine as fixture

SESSION = fixture.SESSION


class WorldCalendarTests(unittest.TestCase):
    def test_public_event_ids_are_stable_across_calls(self):
        rows, notes = public_calendar_candidates(["CN", "cn"], 2026)
        self.assertEqual([], notes)
        self.assertTrue(rows)
        first = [row["id"] for row in rows]
        second = [row["id"] for row in public_calendar_candidates(["CN"], 2026)[0]]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)))
        self.assertTrue(all(row.startswith("public-") for row in first))
        self.assertEqual(
            stable_event_id("a", 1), stable_event_id("a", 1)
        )
        self.assertNotEqual(
            stable_event_id("a", 1), stable_event_id("a", 2)
        )

    def test_underlying_hash_randomization_cannot_change_event_ids(self):
        # If this code accidentally regresses to Python's randomized hash(), the
        # recorded literals below will differ in at least one worker process.
        self.assertEqual(
            stable_event_id("CN", "2026-01-01", "元旦", prefix="public"),
            "public-de59cadfa0595e25d7e2",
        )

    def test_original_world_does_not_absorb_public_holidays(self):
        public = [{
            "id": "public-1",
            "title": "公共新年",
            "start": "2026-01-01",
            "end": "2026-01-01",
            "kind": "festival",
            "day_off": True,
            "description": "公共日历",
            "source": "public_calendar",
            "source_urls": [],
        }]
        raw = {
            "world_type": "real",
            "world_name": "模型乱写的世界名",
            "timezone": "UTC",
            "events": [{
                "title": "风祭",
                "start": "2026-05-09",
                "end": "2026-05-10",
                "kind": "festival",
                "source": "inferred",
                "description": "根据原创设定推定",
            }],
        }
        calendar = validate_calendar(
            raw,
            year=2026,
            configured_type="original",
            configured_countries=["CN"],
            preferred_timezone="Asia/Shanghai",
            preferred_world_name="风之世界",
            public_candidates=public,
        )
        self.assertEqual("original", calendar["world_type"])
        self.assertEqual("风之世界", calendar["world_name"])
        self.assertEqual("Asia/Shanghai", calendar["timezone"])
        self.assertEqual(["风祭"], [row["title"] for row in calendar["events"]])

    def test_real_world_keeps_public_dates_and_downgrades_fake_provenance(self):
        public = [{
            "id": "public-1",
            "title": "公共新年",
            "start": "2026-01-01",
            "end": "2026-01-01",
            "kind": "festival",
            "day_off": True,
            "description": "公共日历",
            "source": "public_calendar",
            "source_urls": [],
        }]
        raw = {
            "world_type": "real",
            "events": [{
                "title": "伪造的公共节日",
                "start": "2026-02-02",
                "end": "2026-02-02",
                "source": "public_calendar",
            }],
        }
        calendar = validate_calendar(
            raw,
            year=2026,
            configured_type="real",
            configured_countries=["CN"],
            public_candidates=public,
        )
        by_title = {row["title"]: row for row in calendar["events"]}
        self.assertEqual("public_calendar", by_title["公共新年"]["source"])
        self.assertEqual("inferred", by_title["伪造的公共节日"]["source"])


class CalendarDiaryEngineTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_calendar_discards_a_draft_when_the_persona_changes(self):
        async def generate(*_args, **_kwargs):
            self.persona.text = "另一个完全不同的人设"
            return json.dumps(
                {
                    "world_type": "original",
                    "world_name": "旧人设的世界",
                    "events": [
                        {
                            "title": "旧人设的纪念日",
                            "start": "2023-11-15",
                            "end": "2023-11-15",
                            "source": "inferred",
                        }
                    ],
                },
                ensure_ascii=False,
            )

        self.engine._ask_journal = AsyncMock(side_effect=generate)
        result = await self.engine.ensure_calendar(
            SESSION, 2023, automatic=True
        )
        self.assertIsNone(result)
        self.assertIsNone(await self.engine.calendar_for_year(SESSION, 2023))

    async def test_concurrent_calendar_requests_share_one_generation(self):
        self.engine.world.calendar.auto_generate = True
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def generate(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return json.dumps(
                {
                    "world_type": "original",
                    "events": [
                        {
                            "title": "风祭",
                            "start": "2023-05-01",
                            "end": "2023-05-01",
                            "source": "inferred",
                        }
                    ],
                },
                ensure_ascii=False,
            )

        self.engine._ask_journal = AsyncMock(side_effect=generate)
        first = asyncio.create_task(
            self.engine.ensure_calendar(SESSION, 2023, automatic=True)
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            self.engine.ensure_calendar(SESSION, 2023, automatic=True)
        )
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(1, calls)
        self.assertEqual(results[0]["year"], results[1]["year"])

    async def test_background_maintenance_makes_at_most_one_new_model_request(self):
        self.engine.world.calendar.auto_generate = True
        self.engine.world.diary.automatic = True
        self.engine.world.diary.write_hour = 0
        self.engine.ensure_calendar = AsyncMock(return_value={"year": 2023})
        self.engine.ensure_diary = AsyncMock(return_value={"day": "2023-11-14"})

        await self.engine.maintain_calendar_diaries()
        self.engine.ensure_calendar.assert_awaited_once()
        self.engine.ensure_diary.assert_not_awaited()

        yesterday = (
            self.engine.local_now().date() - timedelta(days=1)
        ).isoformat()
        _, persona_key = await self.engine._journal_persona(SESSION)
        with self.engine.config_scope(SESSION):
            scope_id = self.engine._journal_scope()
        await self.db.call(
            "calendar_year_save",
            scope_id,
            persona_key,
            self.engine.local_now().year,
            {
                "year": self.engine.local_now().year,
                "events": [],
                "_generation": "model",
                "_config_signature": self.engine._calendar_config_signature(
                    self.engine.world.calendar, self.engine.local_now().year
                ),
            },
        )
        start, _ = self.engine._journal_day_bounds(yesterday)
        await self.db.call(
            "add_event",
            session_id=SESSION,
            persona_id=persona_key,
            event_type="action_done",
            detail={"desc": "安静地读完了书"},
        )
        self.db.raw._execute(
            "UPDATE event_log SET created_at=? WHERE session_id=?",
            (start.timestamp() + 60, SESSION),
        )
        self.clock.advance(601)
        await self.engine.maintain_calendar_diaries()
        self.assertEqual(1, self.engine.ensure_calendar.await_count)
        self.engine.ensure_diary.assert_awaited_once()

    async def test_diary_sources_exclude_other_sessions_and_dialogue_thoughts(self):
        local = self.engine.local_now()
        day = local.date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona_key = await self.engine._journal_persona(SESSION)
        await self.db.call(
            "add_event",
            session_id=SESSION,
            persona_id=persona_key,
            event_type="action_done",
            detail={
                "desc": "自己在窗边读完了一页",
                "quote": "聊天里的私密原话",
                "user_id": "123456",
            },
        )
        await self.db.call(
            "add_event",
            session_id=SESSION,
            persona_id="another-persona",
            event_type="action_done",
            detail={"desc": "另一个人格留下的生活"},
        )
        await self.db.call(
            "add_event",
            session_id="other:GroupMessage:9",
            persona_id=persona_key,
            event_type="action_done",
            detail={"desc": "别的会话隐私"},
        )
        # add_event uses wall-clock time; move both rows into the world-day
        # window so the test remains independent of the machine's date.
        marker = "自己在窗边读完了一页"
        self.db.raw._execute(
            "UPDATE event_log SET created_at=? WHERE session_id=? AND detail LIKE ?",
            (start.timestamp() + 60, SESSION, f"%{marker}%"),
        )
        self.db.raw._execute(
            "UPDATE event_log SET created_at=? WHERE session_id=?",
            (start.timestamp() + 60, "other:GroupMessage:9"),
        )
        async with self.engine.session_state(SESSION) as state:
            state.thoughts = [
                {
                    "at": start.timestamp() + 120,
                    "content": "窗外的风很轻",
                    "persona_key": persona_key,
                },
                {
                    "at": start.timestamp() + 130,
                    "content": "聊天后留下的秘密感想",
                    "dialogue_session": SESSION,
                    "persona_key": persona_key,
                },
                {
                    "at": start.timestamp() + 140,
                    "content": "翻消息留下的感想",
                    "browse_session": SESSION,
                    "persona_key": persona_key,
                },
                {
                    "at": start.timestamp() + 150,
                    "content": "另一个人格的内心",
                    "persona_key": "another-persona",
                },
            ]
            self.engine.add_life_matter(
                state,
                "为当前人格修好风车",
                session=SESSION,
                persona_key=persona_key,
            )
            self.engine.add_life_matter(
                state,
                "另一个人格写下的计划",
                session=SESSION,
                persona_key="another-persona",
            )
        sources = await self.engine._diary_sources(SESSION, day)
        self.assertIn(marker, json.dumps(sources["events"], ensure_ascii=False))
        self.assertNotIn("别的会话隐私", json.dumps(sources, ensure_ascii=False))
        self.assertNotIn("另一个人格", json.dumps(sources, ensure_ascii=False))
        self.assertNotIn("聊天里的私密原话", json.dumps(sources, ensure_ascii=False))
        self.assertNotIn("123456", json.dumps(sources, ensure_ascii=False))
        self.assertNotIn("另一个人格写下的计划", json.dumps(sources, ensure_ascii=False))
        self.assertIn("为当前人格修好风车", json.dumps(sources["matters"], ensure_ascii=False))
        self.assertEqual(["窗外的风很轻"], sources["thoughts"])

    async def test_include_inner_switch_removes_only_private_thoughts(self):
        local = self.engine.local_now()
        day = local.date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona_key = await self.engine._journal_persona(SESSION)
        async with self.engine.session_state(SESSION) as state:
            state.thoughts = [
                {
                    "at": start.timestamp() + 10,
                    "content": "今天想安静一点",
                    "persona_key": persona_key,
                }
            ]
        self.engine.world.diary.include_inner = False
        sources = await self.engine._diary_sources(SESSION, day)
        self.assertEqual([], sources["thoughts"])

    async def test_event_outcome_is_kept_but_chat_fields_are_not(self):
        day = self.engine.local_now().date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona_key = await self.engine._journal_persona(SESSION)
        await self.db.call(
            "add_event",
            session_id=SESSION,
            persona_id=persona_key,
            event_type="event_result",
            detail={
                "title": "旧钟楼的风声停了",
                "outcome": "她找回了齿轮",
                "quote": "不要记录这句聊天",
                "user_id": "998877",
            },
        )
        self.db.raw._execute(
            "UPDATE event_log SET created_at=? WHERE session_id=?",
            (start.timestamp() + 60, SESSION),
        )
        sources = await self.engine._diary_sources(SESSION, day, persona_key)
        serialized = json.dumps(sources["events"], ensure_ascii=False)
        self.assertIn("她找回了齿轮", serialized)
        self.assertNotIn("不要记录这句聊天", serialized)
        self.assertNotIn("998877", serialized)

    async def test_diary_folds_only_adjacent_duplicate_events(self):
        day = self.engine.local_now().date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona_key = await self.engine._journal_persona(SESSION)
        descriptions = ["风车停了", "风车停了", "风车修好了", "风车停了"]
        rows = [
            (SESSION, persona_key, 0, "action_done",
             json.dumps({"desc": desc}, ensure_ascii=False), start.timestamp() + index)
            for index, desc in enumerate(descriptions)
        ]
        with self.db.raw._lock:
            self.db.raw._conn.executemany(
                "INSERT INTO event_log(session_id,persona_id,world_time,event_type,"
                "detail,created_at) VALUES(?,?,?,?,?,?)", rows,
            )
            self.db.raw._conn.commit()
        sources = await self.engine._diary_sources(SESSION, day, persona_key)
        self.assertEqual(
            ["desc: 风车停了", "desc: 风车修好了", "desc: 风车停了"],
            [item["detail"] for item in sources["events"]],
        )
        self.assertEqual([2, 1, 1], [item.get("occurrences", 1) for item in sources["events"]])

    async def test_busy_days_keep_the_newest_event_window(self):
        day = self.engine.local_now().date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona_key = await self.engine._journal_persona(SESSION)
        rows = [
            (
                SESSION,
                persona_key,
                0,
                "action_done",
                json.dumps({"desc": f"{index:03d}"}),
                start.timestamp() + index,
            )
            for index in range(301)
        ]
        with self.db.raw._lock:
            self.db.raw._conn.executemany(
                "INSERT INTO event_log(session_id,persona_id,world_time,event_type,"
                "detail,created_at) VALUES(?,?,?,?,?,?)",
                rows,
            )
            self.db.raw._conn.commit()
        events = await self.db.call(
            "event_log_between",
            [SESSION],
            start.timestamp(),
            start.timestamp() + 400,
            ["action_done"],
            300,
            persona_key,
        )
        self.assertEqual(300, len(events))
        self.assertEqual("001", events[0]["detail"]["desc"])
        self.assertEqual("300", events[-1]["detail"]["desc"])

    async def test_logged_life_events_carry_the_current_persona_key(self):
        state = await self.engine.load_state(SESSION, cold_start=False)
        await self.engine._log_event(
            state, "action_done", {"desc": "给窗台的薄荷浇水"}
        )
        _, persona_key = await self.engine._journal_persona(SESSION)
        rows = await self.db.call(
            "query_events", session_id=SESSION, event_type="action_done", limit=10
        )
        self.assertEqual(persona_key, rows[0]["persona_id"])

    async def test_life_context_is_optional_and_fails_closed(self):
        self.engine.world.life_context.enabled = False
        self.assertEqual("", await self.engine.life_context_text(SESSION))
        self.engine.world.life_context.enabled = True
        self.engine.calendar_for_year = AsyncMock(side_effect=RuntimeError("db down"))
        self.assertEqual("", await self.engine.life_context_text(SESSION))

    async def test_disabling_calendar_hides_cached_events_everywhere(self):
        _, persona_key = await self.engine._journal_persona(SESSION)
        day = self.engine.local_now().date().isoformat()
        await self.db.call(
            "calendar_year_save",
            self.engine._journal_scope(),
            persona_key,
            self.engine.local_now().year,
            {
                "year": self.engine.local_now().year,
                "world_type": "real",
                "_config_signature": self.engine._calendar_config_signature(
                    self.engine.world.calendar, self.engine.local_now().year
                ),
                "events": [
                    {
                        "title": "缓存中的节日",
                        "start": day,
                        "end": day,
                        "source": "public_calendar",
                    }
                ],
            },
        )
        self.engine.world.calendar.enabled = False
        self.assertIsNone(
            await self.engine.calendar_for_year(
                SESSION, self.engine.local_now().year
            )
        )
        self.assertEqual("", await self.engine.life_context_text(SESSION))
        week = await self.engine.life_week(SESSION, day)
        self.assertIsNone(week["calendar"])
        self.assertFalse(any(row["calendar"] for row in week["days"]))

    async def test_changing_world_type_invalidates_the_cached_calendar(self):
        _, persona_key = await self.engine._journal_persona(SESSION)
        year = self.engine.local_now().year
        await self.db.call(
            "calendar_year_save",
            self.engine._journal_scope(),
            persona_key,
            year,
            {
                "year": year,
                "world_type": "real",
                "events": [],
                "_config_signature": self.engine._calendar_config_signature(
                    self.engine.world.calendar, year
                ),
            },
        )
        self.assertIsNotNone(await self.engine.calendar_for_year(SESSION, year))
        self.engine.world.calendar.world_type = "original"
        self.assertIsNone(await self.engine.calendar_for_year(SESSION, year))

    async def test_disabling_diary_hides_existing_entries_and_context(self):
        _, persona_key = await self.engine._journal_persona(SESSION)
        today = self.engine.local_now().date().isoformat()
        await self.db.call(
            "diary_upsert",
            self.engine._journal_scope(),
            persona_key,
            today,
            {"title": "今天的回顾", "mood": "", "content": "这是已经生成的日记。"},
        )
        self.engine.world.diary.enabled = False
        self.assertEqual("", await self.engine.life_context_text(SESSION))
        week = await self.engine.life_week(SESSION, today)
        self.assertTrue(all(row["diary"] is None for row in week["days"]))

    async def test_prompt_places_calendar_and_diary_in_runtime_layer(self):
        state = await self.engine.load_state(SESSION, cold_start=False)
        text = self.engine.prompts.build_autonomous_system_prompt(
            persona_text="喜欢看云的人",
            state=state,
            node=self.engine.node(state.node_id),
            available_tools={},
            life_context="# 今天的日历与文化背景\n夏至",
        )
        self.assertIn("今天的日历与文化背景", text)
        self.assertLess(text.index("今天的日历与文化背景"), text.index("# 这里让你想起"))

    async def test_week_view_reads_events_from_both_sides_of_new_year(self):
        _, persona_key = await self.engine._journal_persona(SESSION)
        scope_id = self.engine._journal_scope()
        await self.db.call(
            "calendar_year_save",
            scope_id,
            persona_key,
            2026,
            {
                "year": 2026,
                "world_type": "original",
                "_config_signature": self.engine._calendar_config_signature(
                    self.engine.world.calendar, 2026
                ),
                "events": [
                    {
                        "id": "e1",
                        "title": "岁末灯会",
                        "start": "2026-12-31",
                        "end": "2026-12-31",
                        "kind": "festival",
                        "source": "inferred",
                    }
                ],
            },
        )
        await self.db.call(
            "calendar_year_save",
            scope_id,
            persona_key,
            2027,
            {
                "year": 2027,
                "world_type": "original",
                "_config_signature": self.engine._calendar_config_signature(
                    self.engine.world.calendar, 2027
                ),
                "events": [
                    {
                        "id": "e2",
                        "title": "新年晨钟",
                        "start": "2027-01-01",
                        "end": "2027-01-01",
                        "kind": "festival",
                        "source": "model",
                    }
                ],
            },
        )
        await self.db.call(
            "diary_upsert",
            scope_id,
            persona_key,
            "2026-12-31",
            {"title": "灯影", "mood": "安静", "content": "我把这一年的信收进盒子里。"},
        )
        week = await self.engine.life_week(SESSION, "2026-12-30")
        self.assertEqual("2026-12-28", week["week_start"])
        self.assertEqual("2027-01-03", week["week_end"])
        self.assertEqual(7, len(week["days"]))
        self.assertEqual("岁末灯会", week["days"][3]["calendar"][0]["title"])
        self.assertEqual("新年晨钟", week["days"][4]["calendar"][0]["title"])
        self.assertEqual("灯影", week["days"][3]["diary"]["title"])

    async def test_week_view_is_not_limited_to_the_thirty_latest_diaries(self):
        _, persona_key = await self.engine._journal_persona(SESSION)
        scope_id = self.engine._journal_scope()
        await self.db.call(
            "diary_upsert",
            scope_id,
            persona_key,
            "2025-01-06",
            {"title": "很久以前", "mood": "", "content": "旧日记仍然应该出现。"},
        )
        for index in range(45):
            day = (
                self.engine.local_now().date() - timedelta(days=index + 10)
            ).isoformat()
            await self.db.call(
                "diary_upsert",
                scope_id,
                persona_key,
                day,
                {"title": f"最近 {index}", "mood": "", "content": "较新的日记"},
            )
        week = await self.engine.life_week(SESSION, "2025-01-06")
        self.assertEqual("很久以前", week["days"][0]["diary"]["title"])

    async def test_past_week_agenda_uses_recorded_schedule_not_current_config(self):
        raw = self.store.raw_schedules()
        raw["schedules"].append(
            {
                "id": "changed_later",
                "time": "19:00",
                "days": ["mon"],
                "enabled": True,
                "note": "后来改成的当前日程",
                "action_chain": [{"type": "read"}],
            }
        )
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.clock._now = datetime(
            2027, 1, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp()
        _, persona_key = await self.engine._journal_persona(SESSION)
        at = datetime(
            2027, 1, 4, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp()
        await self.db.call(
            "add_event",
            session_id=SESSION,
            persona_id=persona_key,
            event_type="schedule",
            detail={
                "id": "changed_later",
                "time": "19:00",
                "note": "当时真正执行的日程",
                "status": "ok",
            },
        )
        self.db.raw._execute(
            "UPDATE event_log SET created_at=? WHERE session_id=?",
            (at, SESSION),
        )
        week = await self.engine.life_week(SESSION, "2027-01-04")
        monday = week["days"][0]
        self.assertEqual("recorded", monday["agenda_source"])
        self.assertEqual("当时真正执行的日程", monday["agenda"][0]["title"])


class CalendarDiaryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp

    async def call(self, endpoint, *, method="GET", **data):
        request = types.SimpleNamespace(
            method=method, query=data, json=AsyncMock(return_value=data)
        )
        with (
            patch.object(self.module.web.api, "request", request),
            patch.object(self.module.web.life_api, "request", request),
        ):
            return await self.routes[endpoint]()

    async def test_week_and_calendar_endpoints_are_session_scoped(self):
        week = await self.call("life/week", session=self.session, start="2026-10-19")
        self.assertEqual(200, week["status_code"], week)
        self.assertEqual(7, len(week["data"]["days"]))
        self.assertEqual(
            self.plugin.engine.local_now().date().isoformat(),
            week["data"]["today"],
        )
        self.assertEqual(
            self.plugin.engine.world.timezone, week["data"]["timezone"]
        )
        calendar = await self.call("calendar", session=self.session, year=2026)
        self.assertEqual(200, calendar["status_code"], calendar)
        self.assertTrue(calendar["data"]["enabled"])
        self.assertIsNone(calendar["data"]["calendar"])
        missing = await self.call("life/week", start="2026-10-19")
        self.assertEqual(400, missing["status_code"])

    async def test_manual_generation_uses_force_and_returns_grounded_result(self):
        self.plugin.engine.ensure_calendar = AsyncMock(return_value={"year": 2026, "events": []})
        result = await self.call(
            "calendar/generate", method="POST", session=self.session, year=2026
        )
        self.assertEqual(200, result["status_code"], result)
        self.plugin.engine.ensure_calendar.assert_awaited_once_with(
            self.session, 2026, force=True, automatic=False
        )
        yesterday = (
            self.plugin.engine.local_now().date() - timedelta(days=1)
        ).isoformat()
        self.plugin.engine.ensure_diary = AsyncMock(
            return_value={"day": yesterday, "title": "回顾", "content": "正文"}
        )
        result = await self.call(
            "diary/generate", method="POST", session=self.session, day=yesterday
        )
        self.assertEqual(200, result["status_code"], result)
        self.plugin.engine.ensure_diary.assert_awaited_once_with(
            self.session, yesterday, force=True, automatic=False
        )

    async def test_diary_generation_rejects_today_and_future_instead_of_inventing(self):
        today = self.plugin.engine.local_now().date()
        for day in (today, today + timedelta(days=1)):
            result = await self.call(
                "diary/generate",
                method="POST",
                session=self.session,
                day=day.isoformat(),
            )
            self.assertEqual(400, result["status_code"], result)

    async def test_diary_generation_rejects_dates_outside_the_retention_window(self):
        self.plugin.engine.world.diary.keep = 7
        self.plugin.engine.ensure_diary = AsyncMock()
        day = self.plugin.engine.local_now().date() - timedelta(days=20)
        result = await self.call(
            "diary/generate",
            method="POST",
            session=self.session,
            day=day.isoformat(),
        )
        self.assertEqual(400, result["status_code"], result)
        self.plugin.engine.ensure_diary.assert_not_awaited()

