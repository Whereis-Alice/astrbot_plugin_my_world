"""A short presence card follows execution identity, including late display replies."""
from __future__ import annotations

import asyncio
import copy

import pytest

from core.group_card_presence import group_card_override
from core.ports import LLMReply
from tests.test_visit_action_card_20261113 import SESSION, fixture, settle


@pytest.mark.parametrize("shared", [None, {"stage": 1}])
def test_travel_clock_remains_truthful_without_matching_shared_activity(shared):
    plugin, holder, _, _ = fixture(model=False)
    holder["visit"]["shared_activity"] = shared
    holder["visit"]["outing"].update(started_at=180, travel_seconds=80, origin_place="客厅")
    assert asyncio.run(group_card_override(plugin, SESSION))["status"] == "去庭院路上"


def test_future_activity_and_scheduled_action_do_not_claim_execution():
    plugin, holder, _, _ = fixture(model=False)
    holder["visit"]["outing"]["started_at"] = 180
    holder["visit"]["shared_activity"]["action_steps"]["guest"][0].update(started_at=180, until=280)
    status = asyncio.run(group_card_override(plugin, SESSION))["status"]
    assert "等" in status and "喝茶" not in status and "泡红茶" not in status


@pytest.mark.parametrize("status", ["cancelled", "declined", "interrupted"])
def test_terminated_action_never_returns_as_current_work(status):
    plugin, holder, _, _ = fixture(model=False)
    holder["visit"]["shared_activity"]["action_steps"]["guest"][0].update(status=status, ended_at=140)
    assert asyncio.run(group_card_override(plugin, SESSION))["status"] == "喝茶中"


@pytest.mark.parametrize("change", ["action_id", "clock", "stage", "revision", "cancel_restart"])
def test_same_words_with_new_execution_evidence_reject_a_late_model_reply(change):
    async def scenario():
        plugin, holder, jobs, calls = fixture()
        original = plugin.engine._budgeted_generate
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(_channel, **_kwargs):
            started.set()
            await release.wait()
            return LLMReply(text='{"status":"热茶慢慢泡着","basis":"action"}')

        plugin.engine._budgeted_generate = slow
        await group_card_override(plugin, SESSION)
        await started.wait()
        visit = holder["visit"]
        shared = visit["shared_activity"]
        action = shared["action_steps"]["guest"][0]
        if change == "action_id":
            action["id"] = "new-tea"
        elif change == "clock":
            action.update(started_at=120, until=220)
        elif change == "stage":
            visit["outing"]["stage"] = shared["stage"] = action["stage"] = 1
        elif change == "revision":
            shared["revision"] += 1
        else:
            replacement = copy.deepcopy(action)
            action.update(status="cancelled", ended_at=149, control_operation="cancel")
            replacement.update(id="retry-tea", started_at=150, until=250)
            shared["action_steps"]["guest"].append(replacement)
        assert (await group_card_override(plugin, SESSION))["status"] == "泡红茶中"
        release.set()
        await settle(jobs)
        assert all(not entry.get("status") for entry in plugin.engine._visit_card_cache.values())
        plugin.engine._budgeted_generate = original
        await group_card_override(plugin, SESSION)
        await settle(jobs)
        assert (await group_card_override(plugin, SESSION))["status"] == "和艾拉泡红茶"
        assert len(calls) == 1

    asyncio.run(scenario())


def test_elapsed_action_uses_natural_waiting_language_without_a_result_claim():
    plugin, holder, _, _ = fixture(model=False)
    holder["now"] = 201
    status = asyncio.run(group_card_override(plugin, SESSION))["status"]
    assert "等" in status and "确认" not in status and "结果" not in status
    assert "好了" not in status and "做完" not in status
