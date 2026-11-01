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
from core.visits.connections import WorldConnections
from core.visits.rhythm import VisitRhythm


class RhythmHarness(VisitRhythm, WorldConnections):
    def __init__(self):
        self.config = replace(protocol.VisitConfig(), enabled=True, auto_enabled=True)
        self._closed, self._backlog = False, False
        self._lock, self._touched = asyncio.Lock(), {}
        self._random = lambda: 0
        self.day = "2026-11-01"
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp()
        self._state = {"peers": {"friend": {"id": "friend", "scope": "life", "enabled": True,
                       "url": "https://friend.example", "local": {"persona_key": "persona"}}},
                       "draws": {"life": {"rhythm_version": 3}}, "visits": {},
                       "enabled_scopes": ["life"], "connections": {}}
        self.connect("friend", ["friend"])
        self.actor_port = SimpleNamespace(local_date=lambda scope: self.day,
                                          describe=AsyncMock(side_effect=lambda scope: self.description()))
        self.invite = AsyncMock()
        self._save = AsyncMock()

    def connect(self, world, roles):
        self._state["connections"][world] = {"id": world, "enabled": True,
            "url": "https://friend.example", "default_actor": roles[0] if roles else "",
            "actors": [{"id": role, "name": role, "world": world} for role in roles]}

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
            "friend": {"days": {"2026-11-01": {"picked": True, "attempts": 1, "successes": 1}},
                       "last_at": self.now}}


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
        entry.update(picked=False, attempts=0, successes=0)
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
        self.service.connect("second", ["second"])
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_awaited_once_with("life", "second", automatic=True)

    async def test_two_roles_in_one_connected_world_share_its_consumed_opportunity(self):
        self.service.consume()
        peer = self.service._state["peers"]["friend"]
        peer["connection_id"] = "world"
        self.service._state["peers"]["second"] = {**peer, "id": "second"}
        self.service._state["connections"].clear()
        self.service.connect("world", ["friend", "second"])
        record = self.service._state["draws"]["life"]
        record["peer_days"]["world"] = record["peer_days"].pop("friend")
        await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()
        self.service.invite.assert_not_awaited()

    async def test_failed_attempt_can_retry_after_cooldown_without_loading_during_cooldown(self):
        self.service.consume()
        record = self.service._state["draws"]["life"]
        record["peer_days"]["friend"]["days"][self.service.day]["successes"] = 0
        record["next_auto_at"] = self.service.now + 3600
        await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()
        self.service.now += 3601
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_awaited_once_with("life", "friend", automatic=True)

    async def test_disabled_worlds_missing_urls_cooldown_and_backlog_avoid_describe(self):
        for condition in ("disabled", "url", "cooldown", "backlog"):
            with self.subTest(condition=condition):
                service = RhythmHarness()
                if condition == "disabled":
                    service._state["connections"]["friend"]["enabled"] = False
                elif condition == "url":
                    service._state["connections"]["friend"]["url"] = ""
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
        for condition in ("close", "disabled", "auto_disabled", "backlog", "life_disabled"):
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
                    elif condition == "life_disabled":
                        service._state["enabled_scopes"].clear()
                    else:
                        service._backlog = True
                    return service.description()

                service.actor_port.describe.side_effect = changed
                await service._automatic()
                service.invite.assert_not_awaited()
                self.assertEqual(service._state["draws"], {})

    async def test_world_removal_or_disable_during_describe_does_not_migrate(self):
        for condition in ("remove", "disabled", "url"):
            with self.subTest(condition=condition):
                service = RhythmHarness()
                service._state["draws"].clear()

                async def changed(scope):
                    if condition == "remove":
                        service._state["connections"].clear()
                    elif condition == "disabled":
                        service._state["connections"]["friend"]["enabled"] = False
                    else:
                        service._state["connections"]["friend"]["url"] = ""
                    return service.description()

                service.actor_port.describe.side_effect = changed
                await service._automatic()
                service.actor_port.describe.assert_awaited_once()
                service.invite.assert_not_awaited()
                self.assertEqual(service._state["draws"], {})

    async def test_new_participating_life_can_invite_global_world_without_a_binding(self):
        self.service._state["peers"].clear()
        self.service._state["enabled_scopes"] = ["second-life"]
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once_with("second-life")
        self.service.invite.assert_awaited_once_with("second-life", "friend", automatic=True)

    async def test_each_enabled_life_gets_an_independent_global_world_opportunity(self):
        self.service.consume()
        self.service._state["enabled_scopes"].append("second-life")
        await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once_with("second-life")
        self.service.invite.assert_awaited_once_with("second-life", "friend", automatic=True)

    async def test_other_remote_roles_cannot_reopen_a_consumed_world_opportunity(self):
        self.service.consume()
        self.service.connect("friend", ["new-role", "friend"])
        self.service._state["peers"].clear()
        for _ in range(20):
            await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()
        self.service.invite.assert_not_awaited()

    async def test_unchecked_or_removed_life_does_not_load_actor(self):
        self.service._state["enabled_scopes"].clear()
        await self.service._automatic()
        self.service.actor_port.describe.assert_not_awaited()
        self.service.invite.assert_not_awaited()

    async def test_binding_failure_retains_quota_and_waits_before_retrying_preparation(self):
        self.service._state["peers"].clear()
        self.service.invite.side_effect = ValueError("binding unavailable")
        await self.service._automatic()
        record = self.service._state["draws"]["life"]
        self.assertEqual(record["next_auto_at"], self.service.now + 3600)
        self.assertFalse(record.get("peer_days"))
        self.service._save.assert_awaited_once()
        for _ in range(20):
            await self.service._automatic()
        self.service.actor_port.describe.assert_awaited_once()
        self.service.invite.assert_awaited_once()
        self.service.now += 3601
        await self.service._automatic()
        self.assertEqual(self.service.invite.await_count, 2)
        self.assertFalse(record.get("peer_days"))

    async def test_binding_failure_with_zero_interval_waits_at_least_one_poll(self):
        self.service.config = replace(self.service.config, auto_interval_minutes=0)
        self.service.invite.side_effect = ValueError("binding unavailable")
        await self.service._automatic()
        record = self.service._state["draws"]["life"]
        self.assertEqual(record["next_auto_at"], self.service.now + self.service.config.poll_seconds)
        await self.service._automatic()
        self.service.invite.assert_awaited_once()

    async def test_rejected_draw_or_consumed_quota_does_not_save_preparation_cooldown(self):
        for condition in ("not_picked", "consumed", "existing_cooldown", "new_visit", "disabled", "removed"):
            with self.subTest(condition=condition):
                service = RhythmHarness()

                async def rejected(*args, **kwargs):
                    if condition in {"not_picked", "consumed"}:
                        service.consume()
                        if condition == "not_picked":
                            entry = service._state["draws"]["life"]["peer_days"]["friend"]["days"][service.day]
                            entry.update(picked=False, attempts=0, successes=0)
                    elif condition == "existing_cooldown":
                        service._state["draws"]["life"]["next_auto_at"] = service.now + 7200
                    elif condition == "new_visit":
                        service._state["visits"]["new"] = {"id": "new", "scope": "life"}
                    elif condition == "disabled":
                        service._state["enabled_scopes"].clear()
                    else:
                        service._state["connections"].clear()
                    raise ValueError("invitation no longer eligible")

                service.invite.side_effect = rejected
                await service._automatic()
                service._save.assert_not_awaited()
                if condition == "existing_cooldown":
                    self.assertEqual(service._state["draws"]["life"]["next_auto_at"], service.now + 7200)
                else:
                    self.assertNotIn("next_auto_at", service._state["draws"]["life"])


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


class SuccessfulVisitQuotaTests(unittest.TestCase):
    def setUp(self):
        self.service = RhythmHarness()

    def visit(self, key="trip", *, scope="life", peer="friend", arrived=True, automatic=True):
        visit = {"id": key, "scope": scope, "remote": {"id": peer}, "automatic": automatic,
                 "local": {"local_date": self.service.day}, "created_at": self.service.now}
        if arrived:
            visit["arrived_at"] = self.service.now + 60
        self.service._state["visits"][key] = visit
        return visit

    def entry(self, scope="life", peer="friend"):
        return self.service._state["draws"][scope]["peer_days"][peer]["days"][self.service.day]

    def test_pending_or_manual_journey_does_not_consume_daily_successes(self):
        self.service._note_automatic_success(self.visit(arrived=False))
        self.service._note_automatic_success(self.visit("manual", automatic=False))
        self.assertNotIn("peer_days", self.service._state["draws"]["life"])

    def test_arrival_is_counted_once_even_after_presence_was_already_marked(self):
        visit = self.visit()
        visit["presence_marked"] = True
        self.service._note_automatic_success(visit)
        self.service._note_automatic_success(visit)
        self.assertEqual(self.entry()["successes"], 1)
        self.assertTrue(visit["automatic_counted"])

    def test_new_remote_role_uses_the_same_world_limit_but_another_life_is_independent(self):
        peer = self.service._state["peers"]["friend"]
        peer["connection_id"] = "world"
        self.service._state["peers"]["other-role"] = {**peer, "id": "other-role"}
        self.service._note_automatic_success(self.visit(peer="other-role"))
        self.assertFalse(self.service._has_daily_opportunity(
            self.service._daily_record("life"), "world", self.service.day, 1))
        self.service._note_automatic_success(self.visit("another-life", scope="second-life"))
        self.assertEqual(self.entry(peer="world")["successes"], 1)
        self.assertEqual(self.entry(scope="second-life", peer="world")["successes"], 1)

    def test_composite_peer_index_uses_the_matching_local_scope(self):
        base = self.service._state["peers"].pop("friend")
        self.service._state["peers"] = {
            "other-life::friend": {**base, "scope": "other-life", "connection_id": "other-world"},
            "life::friend": {**base, "connection_id": "world"}}
        self.service._note_automatic_success(self.visit())
        self.assertEqual(self.entry(peer="world")["successes"], 1)
        self.assertNotIn("other-world", self.service._state["draws"]["life"]["peer_days"])

    def test_remote_instance_keeps_world_allowance_after_binding_is_removed(self):
        self.service._state["peers"].clear()
        visit = self.visit(peer="world:retired-role")
        visit["remote"]["instance_id"] = "world"
        self.service._note_automatic_success(visit)
        self.assertEqual(self.entry(peer="world")["successes"], 1)
        self.assertEqual(self.service._rhythm_peer_id({"id": "world:new-role", "connection_id": "world"}), "world")

    def test_failed_attempt_retains_daily_draw_and_configured_cooldown(self):
        for minutes in (0, 60, 120):
            with self.subTest(minutes=minutes):
                service = RhythmHarness()
                service.config = replace(service.config, auto_interval_minutes=minutes)
                service._random = Mock(return_value=0)
                peer = {"id": "role", "connection_id": "world"}
                self.assertTrue(service._admit_automatic("life", peer, service.description()))
                record = service._state["draws"]["life"]
                entry = record["peer_days"]["world"]["days"][service.day]
                self.assertEqual(record["next_auto_at"], service.now + minutes * 60)
                self.assertEqual(entry["successes"], 0)
                if minutes:
                    service.now += minutes * 60 - 1
                    self.assertFalse(service._admit_automatic("life", peer, service.description()))
                    service.now += 1
                self.assertTrue(service._admit_automatic("life", peer, service.description()))
                self.assertEqual(entry, {"picked": True, "attempts": 2, "successes": 0})
                service._random.assert_called_once()

    def test_arrival_after_midnight_is_charged_to_the_invitation_day(self):
        visit = self.visit()
        self.service.now += 86400
        self.service._note_automatic_success(visit)
        self.assertEqual(self.entry()["successes"], 1)
        self.assertTrue(self.service._has_daily_opportunity(
            self.service._daily_record("life"), "friend", "2026-11-02", 1))

    def test_legacy_migration_refunds_known_failures_and_retains_trimmed_history_counts(self):
        self.service.consume()
        record = self.service._state["draws"]["life"]
        record["rhythm_version"] = 2
        prior = self.entry()
        prior.pop("successes")
        prior["attempts"] = 3
        self.visit("failed", arrived=False)
        arrived = self.visit("success")
        self.service._daily_record("life")
        self.assertEqual(self.entry()["attempts"], 3)
        self.assertEqual(self.entry()["successes"], 2)
        self.assertTrue(arrived["automatic_counted"])
        self.service._note_automatic_success(arrived)
        self.assertEqual(self.entry()["successes"], 2)

    def test_legacy_roles_merge_into_one_connection_without_redrawing_or_losing_cooldown(self):
        peer = self.service._state["peers"]["friend"]
        peer["connection_id"] = "world"
        self.service._state["peers"]["second"] = {**peer, "id": "second"}
        record = self.service._state["draws"]["life"]
        record.update(rhythm_version=2, next_auto_at=self.service.now + 7200, peer_days={
            key: {"days": {self.service.day: {"picked": True, "attempts": 1}}, "last_at": self.service.now}
            for key in ("friend", "second")})
        self.visit("failed", arrived=False)
        arrived = self.visit("success", peer="second")
        self.service._daily_record("life")
        self.assertEqual(set(record["peer_days"]), {"world"})
        self.assertEqual(self.entry(peer="world"), {"picked": True, "attempts": 2, "successes": 1})
        self.assertEqual(record["next_auto_at"], self.service.now + 7200)
        self.assertTrue(arrived["automatic_counted"])

    def test_legacy_composite_bindings_merge_remote_roles_only_within_the_life(self):
        base = self.service._state["peers"].pop("friend")
        self.service._state["peers"] = {
            "other-life::friend": {**base, "scope": "other-life", "connection_id": "other-world"},
            "life::friend": {**base, "connection_id": "world"},
            "life::second": {**base, "id": "second", "connection_id": "world"}}
        record = self.service._state["draws"]["life"]
        record.update(rhythm_version=2, peer_days={
            role: {"days": {self.service.day: {"picked": True, "attempts": 1}}, "last_at": self.service.now}
            for role in ("friend", "second")})
        self.visit("failed", arrived=False)
        self.visit("success", peer="second")
        self.service._daily_record("life")
        self.assertEqual(set(record["peer_days"]), {"world"})
        self.assertEqual(self.entry(peer="world"), {"picked": True, "attempts": 2, "successes": 1})

    def test_legacy_trimmed_role_retains_known_world_allowance(self):
        self.service._state["peers"].clear()
        self.service._state["connections"].clear()
        self.service.connect("world", ["world:new-role"])
        record = self.service._state["draws"]["life"]
        record.update(rhythm_version=2, peer_days={"world:removed-role": {
            "days": {self.service.day: {"picked": True, "attempts": 1}}, "last_at": self.service.now}})
        self.service._daily_record("life")
        self.assertEqual(set(record["peer_days"]), {"world"})
        self.assertEqual(self.entry(peer="world")["successes"], 1)
        self.assertFalse(self.service._has_daily_opportunity(record, "world", self.service.day, 1))

