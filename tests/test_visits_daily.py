"""Daily invitations are independent for each neighbour and survive process reload."""
from dataclasses import replace
from pathlib import Path
import asyncio
import json
import unittest

from tests import test_visits_protocol as protocol
from core.db import AsyncDatabase
from core.visits import VisitConfig, VisitService


class DailyVisitsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol.VisitProtocolTests.asyncSetUp

    async def add_neighbour(self):
        self.c_actor = protocol.Actor("c")
        db = AsyncDatabase(Path(self.directory.name) / "c.db")
        self.addAsyncCleanup(db.close)
        self.c = VisitService(db, self.b.config, self.c_actor, clock=lambda: self.now)
        await self.c.start()
        self.c.config = replace(self.c.config, public_url=f"http://127.0.0.1:{self.c.transport.bound_port}")
        self.addAsyncCleanup(self.c.close)
        code = await self.c.create_pair_code("life-c")
        peer = await self.a.pair("life-a", self.c.config.public_url, code["code"])
        self.third_peer = peer["id"]
        self.a.config = replace(self.a.config, auto_enabled=True)

    async def test_two_neighbours_each_get_one_chance_with_space_between_them(self):
        await self.add_neighbour()
        self.a_actor.accept = False
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)
        self.now += 1800
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)
        self.now += 1801
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 2)
        visits = list(self.a._state["visits"].values())
        self.assertEqual({v["remote"]["id"] for v in visits}, {self.peer, self.third_peer})
        self.now += 7200
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 2)

    async def test_used_chances_survive_reload_and_reset_on_the_local_next_day(self):
        await self.add_neighbour()
        self.a_actor.accept = False
        await self.a.tick()
        self.now += 3601
        await self.a.tick()
        await self.a.close()
        resumed = VisitService(self.a_db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed.start()
        await resumed.tick()
        self.assertEqual(len(self.a_actor.calls), 2)
        describe = self.a_actor.describe
        async def tomorrow(scope):
            result = await describe(scope)
            result["local_date"] = "2026-11-02"
            return result
        self.a_actor.describe = tomorrow
        self.now += 86400
        await resumed.tick()
        self.now += 3601
        await resumed.tick()
        self.assertEqual(len(self.a_actor.calls), 4)

    async def test_sleep_and_human_chat_do_not_consume_the_days_opportunity(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        self.a_actor.accept = False
        self.a_actor.ready = False
        await self.a.tick()
        self.assertEqual(self.a_actor.calls, [])
        self.a_actor.ready = True
        self.a.touch("life-a")
        await self.a.tick()
        self.assertEqual(self.a_actor.calls, [])
        self.now += 181
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)

    async def test_probability_is_drawn_once_for_each_neighbour_not_once_for_the_whole_life(self):
        await self.add_neighbour()
        self.a.config = replace(self.a.config, auto_probability=.5)
        self.a_actor.accept = False
        draws = iter([.9, .1])
        self.a._random = lambda: next(draws)
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)
        self.now += 3601
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)

    async def test_simultaneous_automatic_requests_cannot_double_spend_one_neighbour(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        self.a_actor.accept = False
        await asyncio.gather(self.a.invite("life-a", self.peer, automatic=True),
                             self.a.invite("life-a", self.peer, automatic=True), return_exceptions=True)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(len(self.a._state["visits"]), 1)

    async def test_human_invitation_can_override_an_already_used_automatic_chance(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        self.a_actor.accept = False
        await self.a.tick()
        await self.a.invite("life-a", self.peer, automatic=False)
        self.assertEqual(len(self.a_actor.calls), 2)

    async def test_friends_automatic_visit_does_not_consume_our_own_invitation(self):
        self.b.config = replace(self.b.config, auto_enabled=True)
        await self.b.tick()
        incoming = next(iter(self.a._state["visits"].values()))
        self.assertFalse(incoming["automatic"])
        for _ in range(24):
            self.now += 65
            await self.a.tick()
            await self.b.tick()
        self.assertIsNone(self.a.active("life-a"))
        self.assertFalse(self.a._state["draws"].get("life-a", {}).get("peer_days"))
        before = len(self.a_actor.calls)
        self.a_actor.accept = False
        self.a.config = replace(self.a.config, auto_enabled=True)
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), before + 1)
        automatic = [v for v in self.a._state["visits"].values() if v["automatic"]]
        self.assertEqual(len(automatic), 1)

    async def test_legacy_history_is_not_reinvited_on_the_upgrade_day(self):
        self.a_actor.accept = False
        old = await self.a.invite("life-a", self.peer, automatic=False)
        self.a._state["visits"][old["id"]]["automatic"] = True
        self.a._state["draws"]["life-a"] = {"day": "2026-11-01", "week": "2026-44", "count": 1}
        self.a.config = replace(self.a.config, auto_enabled=True, auto_interval_minutes=0)
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), 1)

    async def test_used_neighbours_are_not_rechecked_through_invite_every_idle_tick(self):
        await self.add_neighbour()
        self.a_actor.accept = False
        await self.a.tick()
        self.now += 3601
        await self.a.tick()

        async def unexpected_invitation(*args, **kwargs):
            self.fail("Today's consumed opportunities should be filtered before checking or saving again")

        self.a.invite = unexpected_invitation
        self.now += 3601
        await self.a.tick()


class DailyConfigTests(unittest.TestCase):
    def test_default_and_legacy_opt_out(self):
        fresh = VisitConfig.from_mapping({})
        self.assertEqual((fresh.daily_peer_limit, fresh.auto_probability, fresh.auto_interval_minutes), (1, 1, 60))
        self.assertEqual(VisitConfig.from_mapping({"visits_weekly_limit": 0}).daily_peer_limit, 0)
        self.assertEqual(VisitConfig.from_mapping({"visits_weekly_limit": 0, "visits_daily_peer_limit": 1}).daily_peer_limit, 0)

    def test_schema_defaults_do_not_reenable_a_legacy_opt_out(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        # This is the same merge performed by AstrBot when adding new config keys.
        loaded = {name: item["default"] for name, item in schema.items() if "default" in item}
        loaded["visits_weekly_limit"] = 0
        self.assertEqual(VisitConfig.from_mapping(loaded).daily_peer_limit, 0)
        loaded["visits_weekly_limit"] = 1
        self.assertEqual(VisitConfig.from_mapping(loaded).daily_peer_limit, 1)
