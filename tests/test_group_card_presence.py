"""Only the current, arrived character can publish temporary card presence."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from core.group_card_presence import engine_group_card_override, group_card_override
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
