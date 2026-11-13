"""Action cards use current execution evidence and a separate, bounded display model."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from core.group_card_presence import group_card_baseline, group_card_override, visit_card_facts
from core.model_calls import call_context
from core.ports import LLMReply

SESSION = "qq:GroupMessage:123"


def fixture(*, model=True):
    visit = {"id": "visit", "scope": "life", "status": "active", "arrived_at": 90,
             "role": "guest", "host_id": "host", "guest_id": "guest",
             "local": {"persona_key": "persona", "actor_id": "alice", "name": "爱丽丝"},
             "remote": {"id": "friend", "name": "艾拉"},
             "outing_version": 1, "outing": {"version": 1, "stage": 0, "place": "庭院",
                "activity": "喝茶", "started_at": 100, "until": 500},
             "shared_activity": {"version": 1, "stage": 0, "revision": 0, "goal": "喝茶",
                "summary": "", "objects": [], "pending": [], "done": False, "milestones": [],
                "action_steps": {"guest": [{"id": "tea", "stage": 0, "description": "泡红茶",
                    "seconds": 100, "waiting": False, "requires_peer": False,
                    "accepted_at": 100, "started_at": 100, "until": 200, "status": "running"}]}}}
    holder = {"visit": visit, "now": 150, "settings": {"visits_card_enabled": True,
              "visits_card_model_enabled": model, "visits_card_max_length": 12}}
    service = NS(config=NS(enabled=True), _closed=False, _loaded=True,
                 active=lambda _scope: copy.deepcopy(holder["visit"]))
    jobs, calls = {}, []
    async def generate(channel, **kwargs):
        kwargs["_request_guard"]()
        calls.append({"channel": channel, "context": call_context(), **kwargs})
        material = json.loads(kwargs["prompt"])
        facts = material["facts"]
        basis = "action" if facts["action"] else "activity" if facts["activity"] else "phase"
        return LLMReply(text=json.dumps({"status": material["examples"][1], "basis": basis}, ensure_ascii=False))
    def queue(key, work):
        old = jobs.get(key)
        if old and not old.done():
            return old
        task = asyncio.create_task(work())
        jobs[key] = task
        return task
    engine = NS(is_enabled=lambda session: session == SESSION, state_key=lambda _session: "life",
                world=NS(nickname_sync=NS(enabled=True)), visits=service, visits_card=object(),
                _now=lambda: holder["now"], visits_card_options=lambda: holder["settings"],
                _journal_persona=AsyncMock(return_value=("persona text", "persona")),
                queue_background=queue, _budgeted_generate=generate)
    plugin = NS(engine=engine, enabled=True, retired=False)
    return plugin, holder, jobs, calls


async def settle(jobs):
    await asyncio.gather(*jobs.values())


def test_running_action_replaces_visit_label_and_ignores_unexecuted_plan():
    async def scenario():
        plugin, holder, _, calls = fixture(model=False)
        holder["visit"]["action_plans"] = {"guest": "赢得比赛并造好飞机"}
        result = await group_card_override(plugin, SESSION)
        assert result == {"source": "astrbot_plugin_my_world", "token": "visit",
                          "name": "爱丽丝", "status": "泡红茶中"}
        assert not calls
    asyncio.run(scenario())


def test_travel_does_not_publish_destination_activity_and_arrival_changes_status():
    async def scenario():
        plugin, holder, _, _ = fixture(model=False)
        holder["visit"]["outing"].update(started_at=180, travel_seconds=80, origin_place="客厅")
        assert (await group_card_override(plugin, SESSION))["status"] == "去庭院路上"
        holder["now"] = 190
        assert (await group_card_override(plugin, SESSION))["status"] == "泡红茶中"
    asyncio.run(scenario())


def test_elapsed_clock_shows_confirmation_and_settled_action_is_not_still_running():
    async def scenario():
        plugin, holder, _, _ = fixture(model=False)
        holder["now"] = 200
        assert (await group_card_override(plugin, SESSION))["status"] == "等结果确认"
        row = holder["visit"]["shared_activity"]["action_steps"]["guest"][0]
        row.update(status="elapsed", settled_revision=1)
        assert (await group_card_override(plugin, SESSION))["status"] == "喝茶中"
        holder["visit"]["shared_activity"]["done"] = True
        assert (await group_card_override(plugin, SESSION))["status"] == "商量下一步"
    asyncio.run(scenario())


def test_unaccepted_scene_offer_is_never_a_current_activity():
    plugin, holder, _, _ = fixture(model=False)
    visit = holder["visit"]
    visit["outing"] = {}
    visit["scene"] = {"place": "游乐园", "activity": "坐过山车"}
    visit["outing_offer"] = dict(visit["scene"])
    result = asyncio.run(group_card_override(plugin, SESSION))
    assert result["status"] == "和艾拉见面"
    assert "过山车" not in result["status"]


def test_other_actor_action_is_not_used_as_her_current_work():
    plugin, holder, _, _ = fixture(model=False)
    actions = holder["visit"]["shared_activity"]["action_steps"]
    actions["host"] = actions.pop("guest")
    assert asyncio.run(group_card_override(plugin, SESSION))["status"] == "喝茶中"


def test_display_model_uses_separate_channel_caches_facts_and_never_mutates_visit():
    async def scenario():
        plugin, holder, jobs, calls = fixture()
        before = copy.deepcopy(holder["visit"])
        assert (await group_card_override(plugin, SESSION))["status"] == "泡红茶中"
        assert not calls  # The getter never waits for a model.
        await settle(jobs)
        assert (await group_card_override(plugin, SESSION))["status"] == "和艾拉泡红茶"
        holder["now"] += 10
        for _ in range(5):
            await group_card_override(plugin, SESSION)
        assert len(calls) == 1 and calls[0]["channel"] is plugin.engine.visits_card
        assert calls[0]["context"]["purpose"] == "visit_card"
        assert calls[0]["max_tokens"] == 100
        assert holder["visit"] == before
    asyncio.run(scenario())


def test_valid_new_wording_is_accepted_and_own_name_and_separator_are_removed():
    async def scenario():
        plugin, _, jobs, _ = fixture()
        plugin.engine._budgeted_generate = AsyncMock(return_value=LLMReply(
            text='{"status":"爱丽丝｜热茶慢慢泡着","basis":"action"}'))
        await group_card_override(plugin, SESSION)
        await settle(jobs)
        assert (await group_card_override(plugin, SESSION))["status"] == "热茶慢慢泡着"
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ['{"status":"造好飞机并获胜","basis":"action"}',
                                  '{"status":"红茶泡好了","basis":"action"}',
                                  '{"status":"泡茶中","basis":[]}',
                                  '{"status":"喝茶中\\n已经回家","basis":"action"}', '{"status":[]}', "不是JSON"])
def test_unproven_display_model_output_is_rejected_with_bounded_retry(text):
    async def scenario():
        plugin, holder, jobs, _ = fixture()
        generate = AsyncMock(return_value=LLMReply(text=text))
        plugin.engine._budgeted_generate = generate
        await group_card_override(plugin, SESSION)
        await settle(jobs)
        for _ in range(5):
            assert (await group_card_override(plugin, SESSION))["status"] == "泡红茶中"
        assert generate.await_count == 1
        holder["now"] = 201
        assert (await group_card_override(plugin, SESSION))["status"] == "等结果确认"
        await settle(jobs)
        assert generate.await_count == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["action", "return", "persona", "settings"])
def test_slow_generation_cannot_apply_to_a_changed_visit_or_configuration(change):
    async def scenario():
        plugin, holder, jobs, _ = fixture()
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(_channel, **_kwargs):
            started.set()
            await release.wait()
            return LLMReply(text='{"status":"和艾拉泡红茶","basis":"action"}')
        plugin.engine._budgeted_generate = slow
        await group_card_override(plugin, SESSION)
        await started.wait()
        if change == "action":
            holder["visit"]["shared_activity"]["action_steps"]["guest"][0]["description"] = "翻书"
        elif change == "return":
            holder["visit"] = None
        elif change == "persona":
            plugin.engine._journal_persona.return_value = ("new text", "new persona")
        else:
            holder["settings"]["visits_card_model_enabled"] = False
        release.set()
        await settle(jobs)
        assert all(not row.get("status") for row in plugin.engine._visit_card_cache.values())
        result = await group_card_override(plugin, SESSION)
        if change in {"return", "persona"}:
            assert result is None
        else:
            assert result["status"] != "和艾拉泡红茶"
        await settle(jobs)
    asyncio.run(scenario())


def test_new_fact_waiting_on_old_job_can_generate_after_old_job_finishes():
    async def scenario():
        plugin, holder, jobs, calls = fixture()
        started, release = asyncio.Event(), asyncio.Event()
        original = plugin.engine._budgeted_generate
        async def slow(channel, **kwargs):
            started.set()
            await release.wait()
            return LLMReply(text='{"status":"和艾拉泡红茶","basis":"action"}')
        plugin.engine._budgeted_generate = slow
        await group_card_override(plugin, SESSION)
        await started.wait()
        holder["visit"]["shared_activity"]["action_steps"]["guest"][0]["description"] = "翻书"
        assert (await group_card_override(plugin, SESSION))["status"] == "翻书中"
        release.set()
        await settle(jobs)
        plugin.engine._budgeted_generate = original
        assert (await group_card_override(plugin, SESSION))["status"] == "翻书中"
        await settle(jobs)
        assert (await group_card_override(plugin, SESSION))["status"] == "和艾拉翻书"
        assert len(calls) == 1
    asyncio.run(scenario())


def test_length_limit_keeps_travel_marker_and_current_phase_is_not_elapsed_minutes():
    plugin, holder, _, _ = fixture(model=False)
    holder["settings"]["visits_card_max_length"] = 6
    holder["visit"]["outing"].update(place="很远的风车山丘", started_at=180, travel_seconds=80)
    result = asyncio.run(group_card_override(plugin, SESSION))
    assert len(result["status"]) <= 6 and result["status"].endswith("路上")
    first = visit_card_facts(holder["visit"], 150)
    assert visit_card_facts(holder["visit"], 160) == first


def test_disabled_dynamic_display_retains_legacy_presence_and_ending_returns_none():
    async def scenario():
        plugin, holder, _, calls = fixture()
        holder["settings"]["visits_card_enabled"] = False
        assert (await group_card_override(plugin, SESSION))["status"] == "拜访艾拉"
        holder["visit"]["status"] = "completed"
        assert await group_card_override(plugin, SESSION) is None
        assert not calls
    asyncio.run(scenario())


def test_plugin_retirement_during_persona_read_does_not_publish_presence():
    async def scenario():
        plugin, _, jobs, _ = fixture()
        async def retired(_session):
            plugin.retired = True
            return "persona text", "persona"
        plugin.engine._journal_persona = retired
        assert await group_card_override(plugin, SESSION) is None
        await settle(jobs)
    asyncio.run(scenario())


@pytest.mark.parametrize("valid", [True, False])
def test_dynamic_card_restoration_requires_its_exact_successful_visit_write(valid):
    from tests.test_group_card_presence import baseline_fixture

    plugin, state, events = baseline_fixture()
    card = "艾拉 泡红茶中"
    state["bot_current_nickname"] = card
    events[0]["detail"].update(to=card, visit_card={"source": "astrbot_plugin_my_world",
        "token": "visit" if valid else "another-visit", "name": "艾拉", "status": "泡红茶中"})
    plugin.engine.card_visit_history = {"visit": {"id": "visit", "scope": "life", "role": "host",
        "arrived_at": 100, "local": {"name": "艾拉", "persona_key": "current-persona"}}}
    result = asyncio.run(group_card_baseline(plugin, SESSION, card))
    assert result == (state["bot_base_nickname"] if valid else None)
