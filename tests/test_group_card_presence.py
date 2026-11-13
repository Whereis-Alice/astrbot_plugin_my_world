"""Only the current, arrived character can publish temporary card presence."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from core.group_card_presence import engine_group_card_override, group_card_baseline, group_card_override
from core.plugin_compat import FREE_CARD_PLUGIN, group_card_owner


SESSION = "qq:GroupMessage:123"


def fixture():
    visit = {"id": "visit-a", "scope": "life", "status": "active", "arrived_at": 100,
             "role": "guest", "local": {"persona_key": "current-persona"},
             "remote": {"name": "艾拉"}}
    holder = {"visit": visit}
    service = NS(config=NS(enabled=True), _closed=False, _loaded=True,
                 active=lambda _scope: holder["visit"])
    engine = NS(is_enabled=lambda session: session == SESSION, state_key=lambda _session: "life",
                world=NS(nickname_sync=NS(enabled=True)), visits=service,
                _journal_persona=AsyncMock(return_value=("current text", "current-persona")))
    plugin = NS(engine=engine, enabled=True, retired=False)
    return plugin, holder


@pytest.mark.parametrize("status", ["active", "finishing"])
def test_current_arrived_character_publishes_presence(status):
    plugin, holder = fixture()
    holder["visit"]["status"] = status
    assert asyncio.run(group_card_override(plugin, SESSION)) == {
        "source": "astrbot_plugin_my_world", "token": "visit-a", "status": "拜访艾拉"}


def test_presence_publishes_only_the_confirmed_local_public_name():
    plugin, holder = fixture()
    holder["visit"]["local"]["name"] = "  爱丽丝  "
    assert asyncio.run(group_card_override(plugin, SESSION))["name"] == "爱丽丝"
    holder["visit"]["local"]["persona_key"] = "previous-persona"
    assert asyncio.run(group_card_override(plugin, SESSION)) is None


def test_missing_public_name_does_not_leak_an_actor_identifier():
    plugin, holder = fixture()
    holder["visit"]["local"]["actor_id"] = "internal-actor-id"
    assert "name" not in asyncio.run(group_card_override(plugin, SESSION))


@pytest.mark.parametrize("change", ["invitation", "unarrived", "wrong_scope", "wrong_persona", "disabled", "retired"])
def test_incomplete_or_other_character_presence_is_not_published(change):
    plugin, holder = fixture()
    visit = holder["visit"]
    if change == "invitation":
        visit["status"] = "invited"
    elif change == "unarrived":
        visit["arrived_at"] = 0
    elif change == "wrong_scope":
        visit["scope"] = "another-life"
    elif change == "wrong_persona":
        visit["local"]["persona_key"] = "previous-persona"
    elif change == "disabled":
        plugin.enabled = False
    elif change == "retired":
        plugin.retired = True
    assert asyncio.run(group_card_override(plugin, SESSION)) is None


def test_restoring_service_is_unavailable_rather_than_a_false_return_home():
    plugin, _ = fixture()
    plugin.engine.visits._loaded = False
    with pytest.raises(RuntimeError, match="恢复"):
        asyncio.run(group_card_override(plugin, SESSION))


def test_presence_is_rechecked_after_reading_current_persona():
    plugin, holder = fixture()
    async def persona(_session):
        holder["visit"] = None
        return "current text", "current-persona"
    plugin.engine._journal_persona = persona
    assert asyncio.run(group_card_override(plugin, SESSION)) is None


def test_engine_helper_can_use_current_known_persona_without_loading_anything():
    plugin, _ = fixture()
    result = asyncio.run(engine_group_card_override(plugin.engine, SESSION,
                        persona_key="current-persona"))
    assert result["status"] == "拜访艾拉"
    plugin.engine._journal_persona.assert_not_awaited()


def test_free_card_ownership_obeys_current_session_activation():
    star = NS(name=FREE_CARD_PLUGIN, root_dir_name=FREE_CARD_PLUGIN, activated=True,
              star_cls=NS(config={}, _closed=False))
    context = NS(get_all_stars=lambda: [star], get_config=lambda _session: {"plugin_set": ["*"]})
    assert "随心名片" in group_card_owner(context, SESSION)
    context.get_config = lambda _session: {"plugin_set": ["astrbot_plugin_my_world"]}
    assert group_card_owner(context, SESSION) == ""
    context.get_config = lambda _session: {"plugin_set": ["*"]}
    star.star_cls._closed = True
    assert group_card_owner(context, SESSION) == ""


def baseline_fixture():
    plugin, _ = fixture()
    config = plugin.engine.world.nickname_sync
    config.template = "{base} {status}"
    config.event_text = "事件中"
    config.status_map, config.node_status = {}, {}
    config.max_length = 30
    plugin.engine.world.actions, plugin.engine.world.nodes = [], []
    state = {"bot_base_nickname": "艾拉 拒绝贴贴中",
             "bot_current_nickname": "艾拉 拒绝贴贴中 | 事件中"}
    events = [{"session_id": SESSION, "persona_id": "current-persona",
               "detail": {"to": state["bot_current_nickname"], "ok": True}}]
    async def database(method, *args, **kwargs):
        if method == "get_state":
            return dict(state)
        assert method == "query_events" and kwargs["session_id"] == SESSION
        return events
    plugin.engine.db = NS(call=database)
    return plugin, state, events


def test_recover_proven_legacy_suffix_keeps_the_original_complete_name():
    plugin, state, _ = baseline_fixture()
    assert asyncio.run(group_card_baseline(plugin, SESSION, state["bot_current_nickname"])) == state["bot_base_nickname"]


@pytest.mark.parametrize("change", ["manual", "read", "failed", "persona", "session", "locked", "custom", "external"])
def test_legacy_suffix_requires_complete_automatic_write_provenance(change):
    plugin, state, events = baseline_fixture()
    card = state["bot_current_nickname"]
    if change == "manual":
        events[0]["detail"]["manual"] = True
    elif change == "read":
        events[0]["detail"] = {"live": card, "ok": True}
    elif change == "failed":
        events[0]["detail"]["ok"] = False
    elif change == "persona":
        events[0]["persona_id"] = "previous-persona"
    elif change == "session":
        events[0]["session_id"] = "qq:GroupMessage:other"
    elif change == "locked":
        state["bot_nickname_locked"] = True
    elif change == "custom":
        card = "艾拉 | Wonderland"
        state["bot_current_nickname"] = card
        events[0]["detail"]["to"] = card
    elif change == "external":
        state["bot_current_nickname"] = "其他新名字"
    assert asyncio.run(group_card_baseline(plugin, SESSION, card)) is None


def test_legacy_source_is_rechecked_after_async_reads():
    plugin, state, _ = baseline_fixture()
    call = plugin.engine.db.call
    async def changed(method, *args, **kwargs):
        result = await call(method, *args, **kwargs)
        if method == "query_events":
            state["bot_base_nickname"] = "用户的新名字"
        return result
    plugin.engine.db.call = changed
    assert asyncio.run(group_card_baseline(plugin, SESSION, state["bot_current_nickname"])) is None


@pytest.mark.parametrize("change", ["locked", "scope"])
def test_final_persona_read_cannot_leave_a_stale_state_proof(change):
    plugin, state, _ = baseline_fixture()
    calls = 0
    async def persona(_session):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "locked":
                state["bot_nickname_locked"] = True
            else:
                plugin.engine.state_key = lambda _session: "another-life"
        return "current text", "current-persona"
    plugin.engine._journal_persona = persona
    assert asyncio.run(group_card_baseline(plugin, SESSION, state["bot_current_nickname"])) is None
