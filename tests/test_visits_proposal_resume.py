"""An explicitly saved proposal survives the application and outbox boundaries."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from core.visits import VisitService
from core.visits.transport import VisitTransportError
from tests import test_visits_protocol as fixture


class SimulatedRestart(BaseException):
    pass


class VisitProposalResumeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp
    advance = fixture.VisitProtocolTests.advance

    async def suspend(self, boundary="decision", **options):
        original = self.a._decide if boundary == "decision" else self.a._send

        async def crash(*args):
            if boundary == "decision":
                await original(*args)
            raise SimulatedRestart()

        name = "_decide" if boundary == "decision" else "_send"
        with patch.object(self.a, name, side_effect=crash):
            with self.assertRaises(SimulatedRestart):
                await self.a.invite("life-a", self.peer, **options)
        return next(reversed(self.a._state["visits"]))

    async def reload_a(self):
        old = self.a
        await old.close()
        self.a = VisitService(old.db, old.config, self.a_actor, clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(self.a.close)
        await self.a.start()
        self.a.config = replace(self.a.config, public_url=f"http://127.0.0.1:{self.a.transport.bound_port}")
        for peer in self.b._state["peers"].values():
            peer["url"] = self.a.config.public_url
        for connection in self.b._state["connections"].values():
            connection["url"] = self.a.config.public_url

    def proposal_calls(self):
        return sum(phase == "propose" for phase, _ in self.a_actor.calls)

    async def test_saved_decision_resumes_both_directions_without_a_second_proposal(self):
        for index, direction in enumerate(("outgoing", "incoming"), 1):
            with self.subTest(direction=direction):
                visit_id = await self.suspend(direction=direction, reason="一起读完那本书")
                self.assertEqual(self.a._state["visits"][visit_id]["proposal_stage"], "pending")
                self.assertNotIn(visit_id, self.b._state["visits"])
                await self.reload_a()
                await self.a._advance(visit_id)
                self.assertIn(visit_id, self.b._state["visits"])
                await self.advance()
                local = self.a._state["visits"][visit_id]
                remote = self.b._state["visits"][visit_id]
                self.assertEqual(local["status"], "completed")
                self.assertEqual(local["log"], remote["log"])
                self.assertEqual(sum(row["kind"] == "invitation" for row in local["log"]), 1)
                self.assertEqual(self.proposal_calls(), index)

    async def test_prepared_offer_resumes_without_reapplying_its_invitation(self):
        visit_id = await self.suspend("prepared", direction="incoming")
        before = copy.deepcopy(self.a._state["visits"][visit_id]["log"])
        self.assertEqual(len(before), 1)
        await self.reload_a()
        await self.a._advance(visit_id)
        local = self.a._state["visits"][visit_id]
        self.assertEqual(local["proposal_stage"], "sent")
        self.assertEqual(local["log"], before)
        self.assertEqual(self.b._state["visits"][visit_id]["log"], before)
        self.assertEqual(self.proposal_calls(), 1)

    async def test_blank_invitation_uses_its_explicit_stage_without_inventing_words(self):
        original = self.a_actor.decide

        async def blank(scope, phase, visit):
            result = await original(scope, phase, visit)
            if phase == "propose":
                result["say"] = ""
            return result

        self.a_actor.decide = blank
        visit_id = await self.suspend("prepared")
        self.assertEqual(self.a._state["visits"][visit_id]["log"], [])
        await self.reload_a()
        await self.a._advance(visit_id)
        self.assertEqual(self.a._state["visits"][visit_id]["log"], [])
        self.assertEqual(self.b._state["visits"][visit_id]["log"], [])
        self.assertEqual(self.proposal_calls(), 1)

    async def test_cancellation_after_saved_decision_never_contacts_the_neighbour(self):
        visit_id = await self.suspend()
        await self.a.return_home("life-a")
        await self.reload_a()
        await self.a.tick()
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertEqual(self.a._state["visits"][visit_id]["status"], "interrupted")
        self.assertEqual(self.proposal_calls(), 1)

    async def test_cancellation_after_preparing_offer_never_contacts_the_neighbour(self):
        visit_id = await self.suspend("prepared")
        await self.a.return_home("life-a")
        await self.reload_a()
        await self.a.tick()
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertNotIn("proposal_offer", self.a._state["visits"][visit_id])
        self.assertEqual(self.proposal_calls(), 1)

    async def test_pending_and_prepared_recovery_rechecks_closed_reception(self):
        original = self.a_actor.describe

        async def closed(scope):
            local = await original(scope)
            local["reception_open"] = False
            return local

        for boundary in ("decision", "prepared"):
            with self.subTest(boundary=boundary):
                visit_id = await self.suspend(boundary)
                await self.reload_a()
                before = self.proposal_calls()
                with patch.object(self.a_actor, "describe", side_effect=closed):
                    await self.a._advance(visit_id)
                current = self.a._state["visits"][visit_id]
                self.assertEqual(current["status"], "declined")
                self.assertEqual(current["end_reason"], "现在不在接待时间，稍后再约")
                self.assertNotIn(visit_id, self.b._state["visits"])
                self.assertEqual(self.proposal_calls(), before)
                self.now += 65

    async def test_pending_and_prepared_recovery_cannot_send_expired_invitation(self):
        for boundary in ("decision", "prepared"):
            with self.subTest(boundary=boundary):
                visit_id = await self.suspend(boundary)
                self.now = self.a._state["visits"][visit_id]["expires_at"] + 1
                await self.reload_a()
                before = self.proposal_calls()
                await self.a._advance(visit_id)
                self.assertEqual(self.a._state["visits"][visit_id]["status"], "expired")
                self.assertNotIn(visit_id, self.b._state["visits"])
                self.assertEqual(self.proposal_calls(), before)

    async def test_manual_proposal_recovery_can_follow_its_own_human_conversation(self):
        visit_id = await self.suspend("prepared")
        await self.reload_a()
        original = self.a_actor.describe

        async def chat(scope):
            local = await original(scope)
            local["chat_busy"] = True
            return local

        with patch.object(self.a_actor, "describe", side_effect=chat):
            await self.a._advance(visit_id)
        self.assertIn(visit_id, self.b._state["visits"])
        self.assertEqual(self.proposal_calls(), 1)

    async def test_automatic_proposal_recovery_yields_to_human_conversation(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        original = self.a_actor.describe

        async def chat(scope):
            local = await original(scope)
            local["chat_busy"] = True
            return local

        for boundary in ("decision", "prepared"):
            with self.subTest(boundary=boundary):
                self.now += self.a.config.auto_interval_minutes * 60 + 1
                visit_id = await self.suspend(boundary, automatic=True)
                await self.reload_a()
                before = self.proposal_calls()
                with patch.object(self.a_actor, "describe", side_effect=chat):
                    await self.a._advance(visit_id)
                current = self.a._state["visits"][visit_id]
                self.assertEqual(current["status"], "declined")
                self.assertEqual(current["end_reason"], "正在接眼前的交谈，稍后再约")
                self.assertNotIn(visit_id, self.b._state["visits"])
                self.assertEqual(self.proposal_calls(), before)

    async def test_cancellation_while_rechecking_proposal_cannot_send(self):
        original = self.a_actor.describe

        for boundary in ("decision", "prepared"):
            with self.subTest(boundary=boundary):
                visit_id = await self.suspend(boundary)
                await self.reload_a()
                entered, release = asyncio.Event(), asyncio.Event()

                async def slow(scope):
                    local = await original(scope)
                    entered.set()
                    await release.wait()
                    return local

                with patch.object(self.a_actor, "describe", side_effect=slow):
                    task = asyncio.create_task(self.a._advance(visit_id))
                    await entered.wait()
                    await self.a.return_home("life-a")
                    release.set()
                    await task
                self.assertEqual(self.a._state["visits"][visit_id]["status"], "interrupted")
                self.assertNotIn(visit_id, self.b._state["visits"])
                self.assertEqual(self.proposal_calls(), len(self.a._state["visits"]))

    async def test_cancellation_at_actual_send_admission_rejects_the_old_offer(self):
        visit_id = await self.suspend("prepared")
        await self.reload_a()
        original = self.a._send

        async def cancel_before_admission(vid, payload):
            async with self.a._lock:
                await self.a._finish(self.a._state["visits"][vid], "interrupted", "取消邀请")
            return await original(vid, payload)

        with patch.object(self.a, "_send", side_effect=cancel_before_admission):
            with self.assertRaises(ValueError):
                await self.a._advance(visit_id)
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertNotIn("outbox", self.a._state["visits"][visit_id])

    async def test_safe_initial_channel_failure_persists_and_keeps_retry_limit_and_quota(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        error = VisitTransportError("对方串门入口暂不可用（HTTP 503）", category="channel", http_status=503)
        with patch.object(self.a, "_send", side_effect=error) as send:
            pending = await self.a.invite("life-a", self.peer, automatic=True)
            visit_id = pending["id"]
            current = self.a._state["visits"][visit_id]
            self.assertEqual(current["contact_error"], str(error))
            self.assertEqual(current.get("network_failures", 0), 0)
            await self.a._save()
            # The diagnostic survives the existing SQLite journal without any model retry.
            stored = await self.a.db.call("kv_get", "world_visits.v1", {})
            self.assertEqual(stored["visits"][visit_id]["contact_error"], str(error))
            for failures in (1, 2, 3):
                self.now = current["next_at"] + 1
                await self.a.tick()
                self.assertEqual(current["network_failures"], failures)
            self.assertEqual(send.await_count, 4)
        self.assertEqual(current["status"], "interrupted")
        self.assertIn(str(error), current["end_reason"])
        self.assertEqual(self.proposal_calls(), 1)
        key = self.a._rhythm_peer_id(current)
        self.assertEqual(self.a._state["draws"]["life-a"]["peer_days"][key]["days"]["2026-11-01"]["successes"], 0)

    async def test_ordinary_retry_error_clears_the_previous_channel_diagnostic(self):
        safe = VisitTransportError("对方串门入口暂不可用（HTTP 503）", category="channel", http_status=503)
        with patch.object(self.a, "_send", side_effect=safe):
            pending = await self.a.invite("life-a", self.peer)
        current = self.a._state["visits"][pending["id"]]
        self.assertIn("contact_error", current)
        self.now = current["next_at"] + 1
        with patch.object(self.a, "_send", side_effect=ValueError("private validation detail")):
            await self.a.tick()
        self.assertNotIn("contact_error", current)
        self.assertNotIn("private validation detail", str(self.a._visible(current)))
        self.assertEqual(self.proposal_calls(), 1)

    async def test_unfinished_proposal_is_interrupted_without_generating_again(self):
        with patch.object(self.a, "_decide", side_effect=SimulatedRestart):
            with self.assertRaises(SimulatedRestart):
                await self.a.invite("life-a", self.peer)
        visit_id = next(iter(self.a._state["visits"]))
        await self.reload_a()
        await self.a.tick()
        self.assertEqual(self.a._state["visits"][visit_id]["status"], "interrupted")
        self.assertEqual(self.a_actor.calls, [])
        self.assertNotIn(visit_id, self.b._state["visits"])

    async def test_legacy_unmarked_decision_is_not_guessed_or_reoffered(self):
        visit_id = await self.suspend()
        local = self.a._state["visits"][visit_id]
        local.pop("proposal_stage")
        local.pop("decision_cursor")
        await self.a._save()
        await self.reload_a()
        with self.assertRaises(ValueError):
            await self.a._advance(visit_id)
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertEqual(self.proposal_calls(), 1)

    async def test_saved_proposal_uses_its_final_model_budget_without_regeneration(self):
        self.a.config = replace(self.a.config, max_calls=2)
        self.b.config = replace(self.b.config, max_calls=2)
        visit_id = await self.suspend()
        await self.reload_a()
        await self.a._advance(visit_id)
        self.assertIn(visit_id, self.b._state["visits"])
        self.assertEqual(self.proposal_calls(), 1)

    async def test_legacy_offer_outbox_replays_without_new_proposal_markers(self):
        original = self.a.transport.request
        captured = []

        async def lose(url, body):
            result = await original(url, body)
            if body["payload"]["op"] == "offer":
                captured.append(copy.deepcopy(body))
                raise SimulatedRestart()
            return result

        with patch.object(self.a.transport, "request", side_effect=lose):
            with self.assertRaises(SimulatedRestart):
                await self.a.invite("life-a", self.peer)
        visit_id = next(iter(self.a._state["visits"]))
        current = self.a._state["visits"][visit_id]
        for key in ("proposal_stage", "proposal_offer", "proposal_cursor", "decision_cursor"):
            current.pop(key, None)
        await self.a._save()
        await self.reload_a()
        request = self.a.transport.request

        async def capture(url, body):
            captured.append(copy.deepcopy(body))
            return await request(url, body)

        self.a.transport.request = capture
        await self.a._advance(visit_id)
        self.assertEqual(captured[0], captured[1])
        self.assertNotIn("outbox", self.a._state["visits"][visit_id])
        self.assertEqual(self.proposal_calls(), 1)
        self.assertEqual(len(self.b._state["visits"]), 1)

    async def test_outbox_replay_keeps_the_envelope_and_counts_arrival_once(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        original = self.a.transport.request
        captured = []

        async def lost_reply(url, body):
            result = await original(url, body)
            if body["payload"]["op"] == "offer":
                captured.append(copy.deepcopy(body))
                raise SimulatedRestart()
            return result

        with patch.object(self.a.transport, "request", side_effect=lost_reply):
            with self.assertRaises(SimulatedRestart):
                await self.a.invite("life-a", self.peer, automatic=True)
        visit_id = next(iter(self.a._state["visits"]))
        old_outbox = copy.deepcopy(self.a._state["visits"][visit_id]["outbox"])
        await self.reload_a()
        request = self.a.transport.request

        async def capture(url, body):
            if body["payload"]["op"] == "offer":
                captured.append(copy.deepcopy(body))
            return await request(url, body)

        self.a.transport.request = capture
        await self.a._advance(visit_id)
        self.assertEqual(captured, [old_outbox, old_outbox])
        await self.advance()
        local = self.a._state["visits"][visit_id]
        key = self.a._rhythm_peer_id(local)
        daily = self.a._state["draws"]["life-a"]["peer_days"][key]["days"]["2026-11-01"]
        self.assertEqual(daily["successes"], 1)
        self.assertEqual(self.proposal_calls(), 1)
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertEqual(sum(row["kind"] == "invitation" for row in local["log"]), 1)
