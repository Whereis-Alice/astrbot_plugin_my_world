"""Cross-check chat presence against confirmed action clocks and display facts."""
from __future__ import annotations

import copy
from types import SimpleNamespace as NS
import unittest

import pytest

from tests import fake_astrbot
fake_astrbot.install()

from core.group_card_presence import visit_card_candidates, visit_card_facts  # noqa: E402
from core.visit_presence import presence_text, visit_activity_text, visit_context_identity, visit_context_text  # noqa: E402
from core.visits.activities import apply_action_control, apply_scene_update, record_action  # noqa: E402
from tests import test_visit_shared_activities_20261113 as fixture  # noqa: E402


def present_visit(*, travel=0):
    value = fixture.visit(travel=travel)
    value.update(id="presence-edge", scope="life", role="host", arrived_at=fixture.NOW - 10,
                 local={"id": fixture.HOST, "actor_id": "alice", "name": "爱丽丝", "persona_key": "persona"},
                 remote={"id": fixture.GUEST, "actor_id": "isla", "name": "艾拉", "world": "港湾"})
    return value


def test_missing_travel_extension_never_makes_future_activity_or_action_current():
    value = present_visit(travel=120)
    assert record_action(value, fixture.HOST, fixture.step("摆茶杯", 10), fixture.NOW)
    value["outing"].pop("travel_seconds")  # Older/incomplete extension still has a confirmed future start.
    observed = fixture.NOW + 20
    assert visit_card_facts(value, observed)["phase"] == "scheduled"
    activity = visit_activity_text(value, now=observed)
    assert "尚未开始" in activity and "正在摆茶杯" not in activity
    assert "尚未开始" in presence_text(value, now=observed)


@pytest.mark.parametrize("interrupted", [False, True])
def test_stopped_actions_do_not_become_unconfirmed_results_at_their_old_deadline(interrupted):
    value = present_visit()
    assert record_action(value, fixture.HOST, fixture.step("抬桌子", 30, requires_peer=interrupted), fixture.NOW)
    row = value["shared_activity"]["action_steps"][fixture.HOST][0]
    if interrupted:
        assert record_action(value, fixture.GUEST,
                             fixture.step("抬另一头", 30, requires_peer=True, peer_action_id=row["id"]), fixture.NOW)
        peer = value["shared_activity"]["action_steps"][fixture.GUEST][0]
        assert apply_action_control(value, fixture.GUEST,
                                    {"action_id": peer["id"], "operation": "cancel", "reason": "先把手里的东西放好"},
                                    fixture.NOW + 1)
        assert row["status"] == "interrupted"
    else:
        assert apply_action_control(value, fixture.HOST,
                                    {"action_id": row["id"], "operation": "cancel", "reason": "暂时不搬这张桌子"},
                                    fixture.NOW + 1)
        assert row["status"] == "cancelled"
    observed = fixture.NOW + 100
    assert visit_card_facts(value, observed)["phase"] == "playing"
    activity = visit_activity_text(value, now=observed)
    assert "结果尚未确认" not in activity and "这一步的时间已到" not in activity


def test_done_goal_with_new_selected_cleanup_is_visible_in_current_presence_and_chat_context():
    value = present_visit()
    value["outing"]["activity"] = value["shared_activity"]["goal"] = "擦桌面"
    assert record_action(value, fixture.HOST, fixture.step("擦净桌面", 1), fixture.NOW)
    action = value["shared_activity"]["action_steps"][fixture.HOST][0]
    assert apply_scene_update(value, fixture.HOST,
                              {"summary": "桌面已经擦净", "done": True, "objects": ["擦净的桌面", "用过的抹布"],
                               "action_ids": [action["id"]]}, fixture.NOW + 1)
    assert record_action(value, fixture.HOST, fixture.step("清洗抹布", 30), fixture.NOW + 2)
    observed = fixture.NOW + 3
    facts = visit_card_facts(value, observed)
    assert facts["phase"] == "doing" and facts["action"] == "清洗抹布"
    assert "正在清洗抹布" in visit_activity_text(value, now=observed)
    assert "清洗抹布" in presence_text(value, now=observed)
    assert "清洗抹布" in visit_context_text(value, now=observed)


def test_short_elapsed_card_waits_naturally_without_claiming_the_object_is_ready():
    value = present_visit()
    assert record_action(value, fixture.HOST, fixture.step("泡红茶", 0.125), fixture.NOW)
    facts = visit_card_facts(value, fixture.NOW + 1)
    assert facts["phase"] == "confirmation"
    status = visit_card_candidates(facts, 12)[0]
    assert "等" in status and "结果" not in status and "确认" not in status
    assert "好了" not in status and "完成" not in status


def test_foreground_action_takes_priority_over_a_new_passive_wait_without_mutating_evidence():
    value = present_visit()
    assert record_action(value, fixture.HOST, fixture.step("擦桌面", 30), fixture.NOW)
    assert record_action(value, fixture.HOST, fixture.step("等水烧开", 120, waiting=True), fixture.NOW + 1)
    original = copy.deepcopy(value)
    assert "正在擦桌面" in visit_activity_text(value, now=fixture.NOW + 2)
    assert "正在等水烧开" in presence_text(value, now=fixture.NOW + 31)
    assert "结果尚未确认" not in presence_text(value, now=fixture.NOW + 31)
    assert value == original


def test_context_identity_tracks_selected_actions_and_objects_without_private_or_observation_churn():
    value = present_visit()
    initial = visit_context_identity(value)
    assert record_action(value, fixture.HOST, fixture.step("摆杯子", 1), fixture.NOW)
    selected = visit_context_identity(value)
    assert selected != initial
    value["private"] = {"alice": {"thought": "这个念头不属于公开相处依据"}}
    value["shared_activity"]["observed_at"] = fixture.NOW + 0.5
    assert visit_context_identity(value) == selected
    value["shared_activity"]["objects"] = ["一只已摆好的杯子"]
    assert visit_context_identity(value) != selected


class VisitPresenceClockTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        from tests import test_engine as engine_fixture

        await engine_fixture.EngineTestCase.asyncSetUp(self)
        self.session = engine_fixture.SESSION
        now = self.clock.now()
        self.visit = {"id": "tick-clock", "scope": self.engine.state_key(self.session),
                      "status": "active", "role": "guest", "arrived_at": now - 10,
                      "local": {"name": "爱丽丝"}, "remote": {"name": "艾拉", "world": "港湾"},
                      "outing_version": 1, "outing": {"version": 1, "stage": 0, "place": "公园",
                          "activity": "折纸", "started_at": now + 120, "until": now + 600,
                          "travel_seconds": 120, "origin_place": "门口"}}
        self.engine.visits = NS(config=NS(enabled=True), _loaded=True, _closed=False, active=lambda _: self.visit)

    async def test_ordinary_tick_notes_and_snapshot_use_the_real_travel_clock(self):
        outcome = await self.engine._tick_session(self.session)
        self.assertIn("尚未抵达", outcome.notes[0])
        self.assertIn("尚未抵达", outcome.summary["node_name"])
        self.clock.advance(121)
        arrived = await self.engine._tick_session(self.session)
        self.assertNotIn("尚未抵达", arrived.notes[0])
        self.assertIn("折纸", arrived.summary["node_name"])
        self.assertFalse(self.llm.calls)

    async def test_thinking_tick_notes_use_the_real_travel_clock(self):
        outcome = await self.engine._tick_while_thinking(self.session)
        self.assertIn("尚未抵达", outcome.notes[0])
        self.assertFalse(self.llm.calls)
