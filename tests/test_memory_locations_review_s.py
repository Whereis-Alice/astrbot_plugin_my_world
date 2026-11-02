"""Visits retain confirmed foreign locations without changing their local map index."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot

fake_astrbot.install()

from core.memory_places import memory_location  # noqa: E402
from core.consolidate import ConsolidateResult  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from tests import test_engine as fixture  # noqa: E402
from tests import test_record_sessions_api as records  # noqa: E402
from tests import test_visits_actor as actors  # noqa: E402


class VisitMemoryLocationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp

    def trip(self, role="guest"):
        own = "instance-a:" + self.local["actor_id"]
        friend = self.visit["remote"]["id"]
        trip = copy.deepcopy(self.visit)
        trip.update(status="completed", role=role, outing_version=1,
                    host_id=own if role == "host" else friend,
                    guest_id=friend if role == "host" else own,
                    expires_at=self.clock.now() + 3600)
        offer = {"place": "港口步道", "activity": "一起看潮汐", "minutes": 15}
        begin_activity(trip, offer, self.clock.now())
        begin_activity(trip, {**offer, "place": "灯塔", "activity": "看灯光"}, self.clock.now() + 900)
        return trip

    async def stored(self, trip):
        await self.port.remember(fixture.SESSION, trip)
        rows = await self.db.call("query_memories", session_id=fixture.SESSION)
        return next(row for row in rows if row["source"] == "life_event")

    async def test_guest_and_host_use_the_host_world_and_confirmed_route(self):
        for role in ("guest", "host"):
            with self.subTest(role=role):
                trip = self.trip(role)
                trip["id"] = "location-" + role
                row = await self.stored(trip)
                saved = json.loads(row["context"])["visit_location"]
                host_world = trip["remote" if role == "guest" else "local"]["world"]
                self.assertEqual(saved["world"], host_world)
                self.assertEqual(row["node_id"], self.local["home_node"])
                label = memory_location(row, self.engine.world.node_map())["label"]
                self.assertEqual(label, host_world + " · 港口步道 → 灯塔")
                recalled = self.engine.memory._to_recalled(1., row).render()
                self.assertIn(label, recalled)
                lines = self.engine.consolidator.memory_lines(fixture.SESSION)
                line = next(line for line in lines if line.startswith(f"#{row['id']} "))
                self.assertIn("地点=" + label, line)

    async def test_proposal_scene_and_invalid_route_do_not_become_places(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                trip = self.trip()
                trip["id"] = "proposal-" + str(corrupt)
                trip["scene"]["place"] = "尚未去过的山顶"
                trip["outing_offer"] = {"place": "尚未去过的山顶", "activity": "野餐", "minutes": 20}
                if corrupt:
                    trip["outing"]["route"][0]["until"] = float("nan")
                else:
                    trip.pop("outing")
                row = await self.stored(trip)
                saved = json.loads(row["context"])["visit_location"]
                self.assertEqual(saved["route"], [])
                self.assertEqual(memory_location(row)["label"], "港湾 · 串门 · 具体地点未记")
                self.assertNotIn("山顶", json.dumps(saved, ensure_ascii=False))

    async def test_legacy_visit_keeps_unknown_place_without_guessing_from_dialogue(self):
        legacy = {"source": "life_event", "node_id": self.local["home_node"],
                  "content": "她说已经在阳台等我了", "context": json.dumps({
                      "kind": "world_visit", "peer_world": "港湾", "confirmed": True})}
        result = memory_location(legacy, self.engine.world.node_map())
        self.assertEqual(result["label"], "串门 · 具体地点未记")
        self.assertEqual(result["peer_world"], "港湾")
        self.assertEqual(result["stops"], [])
        self.assertNotIn(self.local["place"], result["label"])

    async def test_same_receipt_accepts_new_location_but_rejects_rewritten_confirmation(self):
        trip = self.trip()
        legacy = copy.deepcopy(trip)
        legacy.pop("outing")
        legacy.pop("outing_version")
        row = await self.stored(legacy)
        memory_id, created = row["id"], row["created_at"]
        row = await self.stored(trip)
        self.assertEqual((row["id"], row["created_at"]), (memory_id, created))
        expected = json.loads(row["context"])["visit_location"]
        self.assertEqual(len(expected["route"]), 2)
        revised = copy.deepcopy(trip)
        revised["outing"]["route"][0]["place"] = "并未去过的山顶"
        revised["log"].append({"seq": 2, "kind": "say", "actor_id": trip["guest_id"], "text": "再见"})
        row = await self.stored(revised)
        self.assertEqual(json.loads(row["context"])["visit_location"], expected)

    async def test_normal_memory_still_uses_its_local_node_name(self):
        result = memory_location({"source": "runtime", "node_id": self.local["home_node"]},
                                 self.engine.world.node_map())
        self.assertEqual(result["label"], self.local["place"])
        self.assertEqual(result["kind"], "node")

    async def test_malformed_saved_location_leaves_the_legacy_visit_place_unknown(self):
        row = {"source": "life_event", "node_id": self.local["home_node"], "context": json.dumps({
            "kind": "world_visit", "visit_location": {"version": 1, "role": ["guest"],
                "world": "港湾", "route": [{"place": "未经确认的地点"}]}})}
        self.assertEqual(memory_location(row)["label"], "串门 · 具体地点未记")

    async def test_sleep_cannot_merge_separate_visits_that_share_a_home_index(self):
        first = await self.stored(self.trip())
        trip = self.trip()
        trip["id"] = "another-trip"
        second = await self.stored(trip)
        self.assertEqual(first["node_id"], second["node_id"])
        result = ConsolidateResult()
        self.engine.consolidator._apply_full(fixture.SESSION, {
            "_review_ids": {first["id"], second["id"]}, "merge": [{
                "keep_id": first["id"], "drop_ids": [second["id"]], "text": "把两次串门合在一起"}]},
            result, now=self.clock.now(), known=set())
        self.assertEqual(result.merged, 0)
        self.assertTrue(result.skipped)
        unchanged = await self.db.call("memory_by_id", first["id"])
        self.assertEqual(unchanged["content"], first["content"])


class MemoryLocationApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = records.RecordSessionsApiTests.asyncSetUp
    call = records.RecordSessionsApiTests.call

    async def test_search_finds_old_literal_match_before_the_500_row_limit(self):
        raw = self.plugin.db.raw
        def add(index, text):
            return raw.add_memory(session_id=self.session, persona_id="", scope="group_persona",
                                  node_id="study", content=text, source="runtime", created_at=index + 1)
        target = add(0, "旧经历里看到 100%_ 的图案")
        for index in range(1, 510):
            add(index, "后来的一段日常 " + str(index))
        result = await self.call("memories", session=self.session, q="100%_")
        self.assertEqual([row["id"] for row in result["data"]["memories"]], [target])
        self.assertEqual(result["data"]["memories"][0]["location"]["kind"], "node")

    async def test_api_exposes_visit_location_without_mutating_the_map_index(self):
        context = json.dumps({"kind": "world_visit", "confirmed": True, "visit_location": {
            "version": 1, "role": "guest", "world": "港湾",
            "route": [{"place": "灯塔", "activity": "看灯光", "started_at": 1000}]}})
        self.plugin.engine.memory.remember(session_id=self.session, persona_id="", node_id="study",
            content="和朋友看了灯光", memory_type="event", source="life_event", context=context)
        result = await self.call("memories", session=self.session)
        row = result["data"]["memories"][0]
        self.assertEqual(row["node_id"], "study")
        self.assertEqual(row["location"]["label"], "港湾 · 灯塔")
