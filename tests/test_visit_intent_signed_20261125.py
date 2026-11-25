"""Independent personal wishes survive real signed visits without crossing peers."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.intent import activity_intent_context  # noqa: E402
from tests import test_visit_shared_flow_20261113 as fixture  # noqa: E402
from tests import test_visits_flow_review_r as reload_fixture  # noqa: E402


HOST_INTENT = {"want": "只属于东道主的想法：听听她今天遇到的事",
               "enough": "只属于东道主的满足：听到一件她愿意分享的小事就好",
               "feeling": "只属于东道主的感受：有些好奇"}
GUEST_INTENT = {"want": "只属于访客的想法：尝一口这次的茶",
                "enough": "只属于访客的满足：尝过这一杯就好",
                "feeling": "只属于访客的感受：想坐着慢慢喝"}
NEXT_INTENT = {"want": "只属于新提议的想法：一起折一只纸鸟",
               "enough": "只属于新提议的满足：折出能站住的一只就好"}


class SignedVisitIntentTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared
    restart = reload_fixture.OutingFlowReviewTests.restart

    async def asyncSetUp(self):
        await fixture.SharedVisitFlowTests.asyncSetUp(self)
        self.packets = []
        self.responses = []
        for side, world in (("guest", self.a), ("host", self.b)):
            original = world.service.transport.request

            async def request(url, body, *, side=side, original=original):
                self.packets.append((side, copy.deepcopy(body)))
                reply = await original(url, body)
                self.responses.append((side, copy.deepcopy(reply)))
                return reply

            world.service.transport.request = request

    async def start_intent_shared(self):
        originals = []
        for world, phase, note in ((self.b, "receive", HOST_INTENT),
                                   (self.a, "turn", GUEST_INTENT)):
            original = world.llm.generate
            originals.append((world, original))

            async def generate(*, original=original, phase=phase, note=note, **kwargs):
                reply = await original(**kwargs)
                if reply.ok and json.loads(kwargs["prompt"]).get("phase") == phase:
                    value = json.loads(reply.text)
                    value["activity_intent"] = copy.deepcopy(note)
                    return replace(reply, text=json.dumps(value, ensure_ascii=False))
                return reply

            world.llm.generate = generate
        try:
            return await self.start_shared()
        finally:
            for world, original in originals:
                world.llm.generate = original

    def assert_public_channel_private(self, host, guest):
        self.assertTrue(self.packets)
        self.assertTrue(any(body["payload"]["op"] == "step" for _, body in self.packets))
        for _, body in self.packets:
            self.assertEqual(len(body["signature"]), 64)
        public = [body for _, body in self.packets] + [body for _, body in self.responses]
        public.extend((self.b.service._wire(host), self.a.service._wire(guest),
                       self.b.service._record_card_snapshot(host),
                       self.a.service._record_card_snapshot(guest)))
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("activity_intent", encoded)
        for note in (HOST_INTENT, GUEST_INTENT, NEXT_INTENT):
            for text in note.values():
                self.assertNotIn(text, encoded)

    async def propose_next(self, visit_id, host):
        self.b.llm.default_reply = json.dumps({"say": "要不要折一只小鸟？", "outing": {
            "place": "书房", "activity": "一起折纸鸟", "minutes": 2.5, "pause_minutes": 1},
            "activity_intent": NEXT_INTENT}, ensure_ascii=False)
        self.assertTrue(await self.b.service.wake_local_experience(visit_id, self.scope,
            host["local"]["actor_id"], host["local"]["persona_key"],
            self.b.clock.now(), seq=1))
        await self.b.service._advance(visit_id)

    async def test_initial_independent_intents_are_local_through_signed_acceptance(self):
        _, host, guest = await self.start_intent_shared()
        self.assertEqual(activity_intent_context(host), {"current": HOST_INTENT})
        self.assertEqual(activity_intent_context(guest), {"current": GUEST_INTENT})
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["stage"], 0)
        for value in GUEST_INTENT.values():
            self.assertNotIn(value, json.dumps(host, ensure_ascii=False))
        for value in HOST_INTENT.values():
            self.assertNotIn(value, json.dumps(guest, ensure_ascii=False))
        self.assert_public_channel_private(host, guest)

    async def test_private_intents_survive_saving_and_restarting_both_http_services(self):
        visit_id, host, guest = await self.start_intent_shared()
        before_calls = (len(self.a.llm.calls), len(self.b.llm.calls))
        before_notes = [copy.deepcopy(value.get("activity_intent_proposal"))
                        for value in (host, guest)]
        await self.b.service._save()
        await self.a.service._save()
        await self.restart(self.b)
        await self.restart(self.a)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), before_calls)
        self.assertEqual([value.get("activity_intent_proposal") for value in (host, guest)],
                         before_notes)
        self.assertEqual(activity_intent_context(host), {"current": HOST_INTENT})
        self.assertEqual(activity_intent_context(guest), {"current": GUEST_INTENT})
        self.assertEqual(host["outing"], guest["outing"])
        self.assert_public_channel_private(host, guest)

    async def test_rejected_new_offer_keeps_the_existing_intent_and_unfinished_activity(self):
        visit_id, host, guest = await self.start_intent_shared()
        previous = copy.deepcopy(host["outing"])
        await self.propose_next(visit_id, host)
        self.assertEqual(activity_intent_context(host),
                         {"current": HOST_INTENT, "pending_proposal": NEXT_INTENT})
        self.assertEqual(activity_intent_context(guest), {"current": GUEST_INTENT})
        self.assertEqual(host["outing"], previous)
        self.a.llm.default_reply = json.dumps({"say": "我想先把这杯喝完，折纸待会儿再说。",
            "outing_accept": False}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertFalse(host.get("outing_offer"))
        self.assertFalse(guest.get("outing_offer"))
        self.assertEqual(activity_intent_context(host), {"current": HOST_INTENT})
        self.assertEqual(activity_intent_context(guest), {"current": GUEST_INTENT})
        self.assertEqual(host["outing"], previous)
        self.assertEqual(guest["outing"], previous)
        self.assertFalse(host["shared_activity"]["done"])
        self.assertEqual(len(host["outing"]["route"]), 1)
        self.assert_public_channel_private(host, guest)

    async def test_accepting_new_offer_binds_each_intent_to_the_new_stage_only(self):
        visit_id, host, guest = await self.start_intent_shared()
        await self.propose_next(visit_id, host)
        guest_next = {"want": "访客独立想试着折方形纸", "feeling": "访客自己觉得现在有兴趣"}
        self.a.llm.default_reply = json.dumps({"say": "好，我想试着折一张方纸。",
            "outing_accept": True, "pause_minutes": 1, "activity_intent": guest_next},
            ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["outing"]["stage"], 1)
        self.assertEqual(guest["outing"], host["outing"])
        self.assertEqual(activity_intent_context(host), {"current": NEXT_INTENT})
        self.assertEqual(activity_intent_context(guest), {"current": guest_next})
        self.assertNotIn("enough", activity_intent_context(guest)["current"])
        self.assertFalse(host["shared_activity"]["done"])
        self.assertFalse(host["shared_activity"]["milestones"])
        self.assert_public_channel_private(host, guest)


if __name__ == "__main__":
    unittest.main()
