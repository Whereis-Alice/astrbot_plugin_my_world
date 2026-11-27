from importlib import import_module
from types import SimpleNamespace

from core.engine.sessions import SessionsMixin
from tests.test_dialogue_inject import MODULE

session_transport_available = import_module(
    MODULE.__name__ + ".adapters.platform_capabilities"
).session_transport_available


OLD = "old-bot:GroupMessage:123"
OFFICIAL = "official-bot:GroupMessage:85F3DEEAB6E045E18AB54CB46A680549"


def test_adapter_lookup_uses_the_exact_bot_id_and_not_a_display_label():
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official", id="official-bot"))
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        get_platform_inst={"official-bot": platform}.get,
    )
    plugin = SimpleNamespace(context=context)
    assert not session_transport_available(plugin, OLD)
    assert session_transport_available(plugin, OFFICIAL)
    assert session_transport_available(plugin, "a-synthetic-session")
    # Detaching or attaching an adapter is reflected without a plugin reload.
    context.get_platform_inst = dict.fromkeys(["old-bot", "official-bot"], platform).get
    assert session_transport_available(plugin, OLD)


def test_unknown_host_inventory_does_not_pause_existing_lives():
    plugin = SimpleNamespace(context=SimpleNamespace(get_platform_inst=lambda _: None))
    assert session_transport_available(plugin, OFFICIAL)


def _session_engine(grouped=False):
    engine = SessionsMixin()
    rows = [SimpleNamespace(session_id=sid, enabled=True) for sid in (OLD, OFFICIAL)]
    engine.sessions = SimpleNamespace(sessions=rows)
    engine._sessions_index = {row.session_id: row for row in rows}
    engine._group_index = {}
    engine._group_of = {}
    if grouped:
        engine._group_index["shared"] = SimpleNamespace(main_session=OLD, sessions=[OLD, OFFICIAL])
        engine._group_of.update(dict.fromkeys([OLD, OFFICIAL], "shared"))
    engine.world = SimpleNamespace(content_safety=SimpleNamespace(session_blocklist=[]))
    engine.messenger = SimpleNamespace(transport_available=lambda sid: sid == OFFICIAL)
    return engine


def test_inactive_adapter_pauses_its_life_without_changing_the_saved_session():
    engine = _session_engine()
    assert engine.tick_session_ids() == [OFFICIAL]
    assert engine.is_enabled(OLD)
    assert engine.state_key(OLD) == OLD
    engine.messenger.transport_available = lambda _: True
    assert engine.tick_session_ids() == [OLD, OFFICIAL]


def test_a_shared_life_keeps_its_saved_owner_when_a_member_is_still_available():
    engine = _session_engine(grouped=True)
    assert engine.enabled_session_ids() == [OFFICIAL]
    assert engine.tick_session_ids() == [OLD]
    assert engine.state_key(OFFICIAL) == OLD
    engine.messenger.transport_available = lambda _: False
    assert engine.tick_session_ids() == []
    assert engine.state_key(OFFICIAL) == OLD
