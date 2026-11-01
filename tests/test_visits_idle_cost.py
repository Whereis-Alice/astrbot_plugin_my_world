"""Exhausted or unavailable visits avoid repeated state reads without losing tomorrow."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_visits_protocol as protocol
from tests import test_visits_actor as actors
from core.visits.rhythm import VisitRhythm


class RhythmHarness(VisitRhythm):
    def __init__(self):
        self.config = replace(protocol.VisitConfig(), enabled=True, auto_enabled=True)
        self._closed, self._backlog = False, False
        self._lock, self._touched = asyncio.Lock(), {}
        self.day = "2026-11-01"
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp()
        self._state = {"peers": {"friend": {"id": "friend", "scope": "life", "enabled": True,
                       "url": "https://friend.example", "local": {"persona_key": "persona"}}},
                       "draws": {"life": {"rhythm_version": 2}}, "visits": {}}
        self.actor_port = SimpleNamespace(local_date=lambda scope: self.day,
                                          describe=AsyncMock(side_effect=lambda scope: self.description()))
        self.invite = AsyncMock()

    def description(self):
        return {"ready": True, "persona_key": "persona", "local_date": self.day}

    def _now(self):
        return self.now

    def active(self, scope):
        return None

    def _settlement_backlog_full(self, scope):
        return self._backlog

    def consume(self):
        self._state["draws"]["life"]["peer_days"] = {
            "friend": {"days": {"2026-11-01": {"picked": True, "attempts": 1}}, "last_at": self.now}}


class IdleVisitCostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = RhythmHarness()

    async def test_consumed_day_does_not_load_identity_or_state_on_idle_ticks(self):
        self.service.consume()
        for _ in range(20):
            await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()
        self.service.invite.assert_not_awaited()

    async def test_rejected_daily_draw_is_also_exhausted(self):
        self.service.consume()
        entry = self.service._state["draws"]["life"]["peer_days"]["friend"]["days"][self.service.day]
        entry.update(picked=False, attempts=0)
        await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()

    async def test_local_midnight_reopens_opportunity_while_utc_is_still_yesterday(self):
        self.service.consume()
        self.service.now = datetime(2026, 11, 1, 16, 1, tzinfo=timezone.utc).timestamp()
        self.service.day = "2026-11-02"
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once_with("life")
        self.service.invite.assert_awaited_once_with("life", "friend", automatic=True)

    async def test_one_unused_neighbour_keeps_the_life_eligible(self):
        self.service.consume()
        self.service._state["peers"]["second"] = {
            **self.service._state["peers"]["friend"], "id": "second"}
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_awaited_once_with("life", "second", automatic=True)

    async def test_disabled_peers_missing_urls_cooldown_and_backlog_avoid_describe(self):
        for condition in ("disabled", "url", "cooldown", "backlog"):
            with self.subTest(condition=condition):
                service = RhythmHarness()
                if condition == "disabled":
                    service._state["peers"]["friend"]["enabled"] = False
                elif condition == "url":
                    service._state["peers"]["friend"]["url"] = ""
                elif condition == "cooldown":
                    service._state["draws"]["life"]["next_auto_at"] = service.now + 3600
                else:
                    service._backlog = True
                await service._automatic()
                service.actor_port.describe.assert_not_awaited()
                service.invite.assert_not_awaited()

    async def test_optional_calendar_preserves_old_actor_and_migration_paths(self):
        self.service.consume()
        del self.service.actor_port.local_date
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_not_awaited()
        self.service.actor_port.describe.reset_mock()
        self.service._state["draws"]["life"]["rhythm_version"] = 1
        self.service.actor_port.local_date = Mock(side_effect=AssertionError("Legacy rhythm must migrate first"))
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.actor_port.local_date.assert_not_called()

    async def test_disabled_life_calendar_avoids_describe(self):
        self.service.actor_port.local_date = lambda scope: ""
        await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()

    async def test_invalid_fast_calendar_uses_fresh_description(self):
        self.service.actor_port.local_date = lambda scope: "not-a-date"
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_awaited_once()

    async def test_close_or_config_change_during_describe_cannot_invite_or_migrate(self):
        for condition in ("close", "disabled", "auto_disabled", "backlog"):
            with self.subTest(condition=condition):
                service = RhythmHarness()
                service._state["draws"].clear()

                async def changed(scope):
                    if condition == "close":
                        service._closed = True
                    elif condition == "disabled":
                        service.config = replace(service.config, enabled=False)
                    elif condition == "auto_disabled":
                        service.config = replace(service.config, auto_enabled=False)
                    else:
                        service._backlog = True
                    return service.description()

                service.actor_port.describe.side_effect = changed
                await service._automatic()
                service.invite.assert_not_awaited()
                self.assertEqual(service._state["draws"], {})

    async def test_friend_removal_or_disable_during_describe_does_not_migrate(self):
        for condition in ("remove", "disabled", "url"):
            with self.subTest(condition=condition):
                service = RhythmHarness()
                service._state["draws"].clear()

                async def changed(scope):
                    if condition == "remove":
                        service._state["peers"].clear()
                    elif condition == "disabled":
                        service._state["peers"]["friend"]["enabled"] = False
                    else:
                        service._state["peers"]["friend"]["url"] = ""
                    return service.description()

                service.actor_port.describe.side_effect = changed
                await service._automatic()
                service.actor_port.describe.assert_awaited_once()
                service.invite.assert_not_awaited()
                self.assertEqual(service._state["draws"], {})


class LocalVisitCalendarTests(unittest.TestCase):
    def test_adapter_calendar_uses_scope_and_no_database_or_state_loading(self):
        selected = []

        @contextmanager
        def config_scope(scope):
            selected.append(scope)
            try:
                yield
            finally:
                selected.pop()

        engine = SimpleNamespace(state_key=lambda scope: "shared-life", config_scope=config_scope,
            is_enabled=lambda scope: scope == "shared-life",
            local_now=lambda: datetime(2026, 11, 2) if selected == ["shared-life"] else datetime(2026, 11, 1),
            db=SimpleNamespace(call=Mock(side_effect=AssertionError("No database"))),
            load_state=Mock(side_effect=AssertionError("No state load")))
        plugin = SimpleNamespace(engine=engine, enabled=True, retired=False)
        port = actors.VisitActorPort(plugin)
        self.assertEqual(port.local_date("group-session"), "2026-11-02")
        self.assertEqual(selected, [])
        engine.db.call.assert_not_called()
        engine.load_state.assert_not_called()
        plugin.retired = True
        self.assertEqual(port.local_date("group-session"), "")

