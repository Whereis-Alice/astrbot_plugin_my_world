"""Independent final boundary checks; no providers, QQ or deployment side effects."""
from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import pytest

from tests import test_visits_protocol as protocol
from core.visits.protocol import canonical, clean_decision, envelope, message_limit
from core.visits.snapshots import validate_snapshot
from core.visits.history_pages import LOG_PAGE_SIZE

BASE = 1793505600.0
HOST, GUEST = "h:h", "g:g"


def _history_fixture():
    logs = [{"seq": i + 1, "actor_id": HOST if i % 2 else GUEST,
             "kind": "say", "text": f"原话 {i + 1}", "at": BASE + i}
            for i in range(1000)]
    route = [{"place": f"地点 {i}", "activity": f"共同活动 {i}",
              "started_at": BASE + i * 600, "until": BASE + (i + 1) * 600}
             for i in range(71)]
    now = BASE + 72 * 600
    outing = {"version": 1, "stage": 70, **route[-1], "route": route,
              "return_agreed": False, "farewells": {}}
    host = {"id": "a" * 32, "scope": "life", "role": "host", "status": "active",
            "host_id": HOST, "guest_id": GUEST, "local": {"actor_id": "h"},
            "remote": {"id": GUEST}, "automatic": False, "manual": True,
            "outing_version": 1, "created_at": BASE, "arrived_at": BASE,
            "presence_marked": True, "expires_at": now + 3600,
            "next_actor": HOST, "next_at": now, "turns": {HOST: 0, GUEST: 0},
            "calls": {HOST: 0, GUEST: 0}, "max_calls": 2, "turn_limit": 1,
            "event_stage": "none", "scene": {"place": route[-1]["place"],
            "activity": route[-1]["activity"]}, "log": logs, "outing": outing}
    guest = copy.deepcopy(host)
    guest.update(role="guest", local={"actor_id": "g"}, remote={"id": HOST}, log=copy.deepcopy(logs[:-4]))
    prior_route = copy.deepcopy(route[:-1])
    guest["outing"] = {"version": 1, "stage": 69, **prior_route[-1], "route": prior_route,
                       "return_agreed": False, "farewells": {}}
    guest["outing_offer"] = {"place": route[-1]["place"], "activity": route[-1]["activity"],
                              "minutes": 10, "event": "", "return_after": False}
    guest["decision"] = {"outing_accept": True, "return_after": False, "farewell": ""}
    service = protocol.VisitService(None, protocol.VisitConfig(enabled=True, outings_enabled=True),
                                    SimpleNamespace(), clock=lambda: now)
    service._state = {"instance_id": "g", "draws": {}, "presence_epochs": {}}
    return service, host, guest, now


def test_large_snapshot_and_synchronization_keep_all_known_facts():
    service, host, guest, now = _history_fixture()
    wire = service._wire(host)
    assert len(wire["log"]) <= LOG_PAGE_SIZE
    assert wire["log"][0]["seq"] == wire["log_offset"] + 1
    assert wire["outing"]["stage"] == 70
    assert wire["outing"]["route_offset"] == 7
    remote = validate_snapshot(guest, wire, now=now)
    assert remote["log"] == host["log"]
    assert remote["outing"]["route"] == host["outing"]["route"]
    service._synchronize(guest, wire, HOST)
    assert guest["log"] == host["log"]
    assert guest["outing"]["route"] == host["outing"]["route"]


def test_changed_omitted_log_prefix_is_rejected():
    service, host, guest, now = _history_fixture()
    host["log"][0]["text"] = "替换已经确认的原话"
    with pytest.raises(ValueError):
        validate_snapshot(guest, service._wire(host), now=now)


def test_changed_omitted_route_prefix_is_rejected():
    service, host, guest, now = _history_fixture()
    host["outing"]["route"][0]["place"] = "未经双方确认的新地方"
    with pytest.raises(ValueError):
        validate_snapshot(guest, service._wire(host), now=now)


def test_missing_independent_prefix_is_not_reconstructed():
    service, host, guest, now = _history_fixture()
    guest["log"] = guest["log"][:50]
    with pytest.raises(ValueError):
        validate_snapshot(guest, service._wire(host), now=now)


def test_guest_farewell_cannot_be_invented_on_first_confirmation():
    service, host, guest, now = _history_fixture()
    guest["outing_offer"]["return_after"] = True
    guest["decision"].update(return_after=True, farewell="晚些时候再见。")
    host["outing"].update(return_agreed=True, farewells={HOST: "回去歇一会儿。", GUEST: "被替写的原话"})
    with pytest.raises(ValueError):
        validate_snapshot(guest, service._wire(host), now=now)
    host["outing"]["farewells"][GUEST] = guest["decision"]["farewell"]
    assert validate_snapshot(guest, service._wire(host), now=now)["outing"]["return_agreed"]


def test_maximum_legal_chinese_rows_fit_the_selected_wire_limit():
    service, host, _, now = _history_fixture()
    for row in host["log"]:
        row.update(text="汉" * 500, kind="字" * 40)
    for row in host["outing"]["route"]:
        row.update(place="景" * 80, activity="游" * 120)
    host["outing"].update(place=host["outing"]["route"][-1]["place"],
                          activity=host["outing"]["route"][-1]["activity"])
    body = envelope("test-only-key", HOST, GUEST, {"op": "sync", "visit": service._wire(host)}, now)
    assert len(canonical(body)) <= message_limit(body)


def test_personal_activity_experiences_keep_each_speakers_identity():
    service, host, _, _ = _history_fixture()
    service._record_outing_choice(host, HOST, clean_decision({"outcome": "我看了湖边的日落。"}))
    service._record_outing_choice(host, GUEST, clean_decision({"outcome": "我把这一幕记下了。"}))
    actual = [(row["actor_id"], row["text"]) for row in host["log"] if row["kind"] == "experience"]
    assert actual == [(HOST, "我看了湖边的日落。"), (GUEST, "我把这一幕记下了。")]


def test_human_upgrade_preserves_the_two_confirmed_return_choices():
    service, host, _, _ = _history_fixture()
    host.update(manual=False, manual_continuation=True,
                budget_holds={HOST: {"reset_at": BASE + 90000, "reason": "自主预算等待"}})
    host["outing"].update(return_agreed=True, farewells={HOST: "我要回家啦。", GUEST: "下次再见。"})
    before = copy.deepcopy(host["outing"])
    service._resume_manual_activity(host)
    assert host["manual"] is True
    assert not host.get("budget_holds")
    assert host["outing"] == before


class BudgetCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_checkpoint_retains_one_body_and_confirmed_place(self):
        service, host, _, now = _history_fixture()
        host["manual"] = False
        before = copy.deepcopy(host["outing"])
        service._save = AsyncMock()
        await service._hold_outing_budget(host, {"allowed": False, "reset_at": now + 86400,
                                                "reason": "自主日账已用完", "snapshot": ["today"]})
        self.assertEqual(host["status"], "active")
        self.assertEqual(host["outing"], before)
        self.assertEqual(host["next_at"], now)
        self.assertTrue(host["budget_notice_pending"])
        self.assertGreaterEqual(host["expires_at"], now + 86400 + 3600)
        self.assertEqual(service._outing_wait_at(host), now + 86400)
