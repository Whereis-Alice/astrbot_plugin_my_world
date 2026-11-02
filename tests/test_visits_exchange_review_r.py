"""Independent choices stay intact through signed journey snapshots."""
from __future__ import annotations

import copy
import unittest

import pytest

from tests.test_visits_contract_boundaries_q import _history_fixture
from tests import test_visits_outings_q as outing_fixture
from core.visits.outings import pause_activity
from core.visits.protocol import validate_outing_request, validate_outing_request_maps
from core.visits.snapshots import validate_snapshot


def _paused_exchange():
    service, host, guest, now = _history_fixture()
    stop = {"place": "湖边", "activity": "一起散步", "started_at": now - 30,
            "until": now + 9000}
    host["outing"] = {"version": 1, "stage": 0, **stop, "route": [stop],
                      "return_agreed": False, "farewells": {}}
    host["expires_at"] = now + 12600
    pause_activity(host, 120, 3, now)
    guest["outing"] = copy.deepcopy(host["outing"])
    guest["log"] = copy.deepcopy(host["log"])
    guest["expires_at"] = host["expires_at"]
    guest["decision"] = {"pause_minutes": 3}
    return service, host, guest, now


def test_confirmed_guest_pause_cannot_change_under_same_shared_deadline():
    service, host, guest, now = _paused_exchange()
    before = copy.deepcopy(guest)
    altered = service._wire(host)
    altered["outing"]["pause_choices"][guest["guest_id"]] = 30
    # The host's longer pause keeps quiet_until identical, hiding this change
    # from deadline-only checks of the guest's independent choice.
    assert altered["outing"]["quiet_until"] == guest["outing"]["quiet_until"]
    with pytest.raises(ValueError, match="休息意愿"):
        service._synchronize(guest, altered, guest["host_id"])
    assert guest == before


def test_host_cannot_unilaterally_shorten_a_confirmed_shared_pause():
    service, host, guest, now = _paused_exchange()
    pause_activity(host, 1, 3, now)
    assert host["outing"]["quiet_until"] < guest["outing"]["quiet_until"]
    with pytest.raises(ValueError, match="共同确认的休息意愿"):
        validate_snapshot(guest, service._wire(host), now=now)


def test_new_pause_cannot_reuse_an_old_guest_decision_without_a_submitted_reply():
    service, host, guest, now = _paused_exchange()
    pause_activity(host, 120, 3, now + 30)
    with pytest.raises(ValueError, match="独立回应"):
        validate_snapshot(guest, service._wire(host), now=now + 30)


def test_new_pause_checkpoint_checks_own_choice_even_if_activity_end_clamps_deadline():
    service, host, guest, now = _paused_exchange()
    host["outing"]["until"] = now + 600
    host["outing"]["route"][-1]["until"] = now + 600
    pause_activity(host, 120, 3, now)
    guest["outing"] = copy.deepcopy(host["outing"])
    guest["decision"] = {"pause_minutes": 1}
    guest["next_actor"] = guest["guest_id"]
    guest["decision_cursor"] = service._decision_cursor(guest, "turn")
    guest["unsent_turn"] = True
    pause_activity(host, 120, 30, now + 30)
    host["turns"][guest["guest_id"]] += 1
    altered = service._wire(host)
    assert altered["outing"]["quiet_until"] == guest["outing"]["quiet_until"]
    with pytest.raises(ValueError, match="休息意愿"):
        validate_snapshot(guest, altered, now=now + 30)
    host["outing"]["pause_choices"][guest["guest_id"]] = 1
    assert validate_snapshot(guest, service._wire(host), now=now + 30)["outing"]["pause_choices"][guest["guest_id"]] == 1


@pytest.mark.parametrize("changed_proof", ["missing_submission", "stale_log", "stale_turns", "no_turn_advance"])
def test_new_pause_is_bound_to_this_submitted_guest_turn(changed_proof):
    service, host, guest, now = _paused_exchange()
    guest["next_actor"] = guest["guest_id"]
    guest["decision_cursor"] = service._decision_cursor(guest, "turn")
    guest["unsent_turn"] = True
    host["turns"][guest["guest_id"]] += 1
    pause_activity(host, 120, 3, now + 30)
    if changed_proof == "missing_submission":
        guest.pop("unsent_turn")
    elif changed_proof == "stale_log":
        guest["decision_cursor"]["log_seq"] -= 1
    elif changed_proof == "stale_turns":
        guest["decision_cursor"]["turns"][guest["guest_id"]] += 1
    else:
        host["turns"][guest["guest_id"]] -= 1
    with pytest.raises(ValueError, match="独立回应"):
        validate_snapshot(guest, service._wire(host), now=now + 30)


def test_signed_pause_reply_can_complete_when_a_new_suggestion_arrives_in_flight():
    service, host, guest, now = _paused_exchange()
    guest["next_actor"] = guest["guest_id"]
    guest["decision_cursor"] = service._decision_cursor(guest, "turn")
    guest["unsent_turn"] = True
    host["turns"][guest["guest_id"]] += 1
    pause_activity(host, 120, 3, now + 30)
    request = _request("next-discussion", now + 30)
    guest["outing_requests"] = {guest["guest_id"]: request}
    actual = validate_snapshot(guest, service._wire(host), now=now + 30)
    assert actual["outing"] == host["outing"]
    assert actual["outing_requests"] == guest["outing_requests"]


def test_unchanged_checkpoint_does_not_apply_a_new_unconfirmed_guest_choice():
    service, host, guest, now = _paused_exchange()
    guest["decision"] = {"pause_minutes": 1}
    assert validate_snapshot(guest, service._wire(host), now=now)["outing"]["pause_choices"][guest["guest_id"]] == 3


def test_confirmed_pause_choices_cannot_be_removed_or_moved_into_the_future():
    service, host, guest, now = _paused_exchange()
    altered = service._wire(host)
    altered["outing"].pop("pause_choices")
    with pytest.raises(ValueError, match="休息意愿"):
        validate_snapshot(guest, altered, now=now)
    pause_activity(host, 120, 3, now + 301)
    with pytest.raises(ValueError, match="休息节奏"):
        validate_snapshot(guest, service._wire(host), now=now)


def _request(request_id, now, *, seq=1, message="去花园看看"):
    return {"id": request_id, "text": message, "at": now, "seq": seq}


@pytest.mark.parametrize("change", [{"text": "替本人改写"}, {"seq": 3}, {"id": "forged"}])
def test_remote_cannot_rewrite_own_latest_human_request(change):
    service, host, guest, now = _paused_exchange()
    own_id = guest["guest_id"]
    current = _request("guest-latest", now, seq=2)
    guest["outing_requests"] = {own_id: current}
    host["outing_requests"] = {own_id: {**current, **change}}
    with pytest.raises(ValueError, match="本人改写"):
        validate_snapshot(guest, service._wire(host), now=now)


def test_delayed_request_and_acknowledgement_preserve_same_second_newer_request():
    service, host, guest, now = _paused_exchange()
    own_id = guest["guest_id"]
    guest["outing_requests"] = {own_id: _request("guest-latest", now, seq=2)}
    guest["outing_requests_handled"] = {own_id: "guest-previous"}
    host["outing_requests"] = {own_id: _request("guest-previous", now)}
    host["outing_requests_handled"] = {own_id: "guest-previous"}
    actual = validate_snapshot(guest, service._wire(host), now=now)
    assert actual["outing_requests"] == guest["outing_requests"]
    assert actual["outing_requests_handled"] == guest["outing_requests_handled"]


def test_new_acknowledgement_requires_the_guests_independent_reply_for_both_requests():
    service, host, guest, now = _paused_exchange()
    requests = {guest["guest_id"]: _request("guest-current", now),
                guest["host_id"]: _request("host-current", now)}
    host["outing_requests"] = copy.deepcopy(requests)
    guest["outing_requests"] = copy.deepcopy(requests)
    host["outing_requests_handled"] = {actor: request["id"] for actor, request in requests.items()}
    with pytest.raises(ValueError, match="独立回应"):
        validate_snapshot(guest, service._wire(host), now=now)
    guest["unsent_turn"] = True
    guest["decision_cursor"] = {"outing_request_turn": copy.deepcopy(host["outing_requests_handled"])}
    actual = validate_snapshot(guest, service._wire(host), now=now)
    assert actual["outing_requests_handled"] == host["outing_requests_handled"]
    guest["outing_requests_handled"] = actual["outing_requests_handled"]
    guest.pop("unsent_turn")
    assert validate_snapshot(guest, service._wire(host), now=now)["outing_requests_handled"] == host["outing_requests_handled"]


def test_signed_request_receiver_orders_same_second_requests_and_locks_confirmed_content():
    service, host, _, now = _paused_exchange()
    actor = host["guest_id"]
    old, latest = _request("old", now), _request("latest", now, seq=2)
    service._accept_outing_request(host, actor, {"human_request": latest})
    service._accept_outing_request(host, actor, {"human_request": old})
    assert host["outing_requests"][actor] == latest
    before = copy.deepcopy(host)
    with pytest.raises(ValueError, match="不能被改写"):
        service._accept_outing_request(host, actor, {"human_request": {**latest, "text": "改写"}})
    assert host == before


def test_request_maps_remain_bounded_and_sender_identity_is_not_in_the_body():
    request = _request("one", 100)
    with pytest.raises(ValueError, match="请求无效"):
        validate_outing_request({**request, "actor_id": "other"})
    with pytest.raises(ValueError, match="来源无效"):
        validate_outing_request_maps({"outing_requests": {"h": request, "g": request, "third": request}}, {"h", "g"})


class SignedOutingRequestTests(unittest.IsolatedAsyncioTestCase):
    make_world = outing_fixture.OutingClockTests.make_world
    exhaust = outing_fixture.OutingClockTests.exhaust
    advance = outing_fixture.OutingClockTests.advance
    guest_reply = outing_fixture.OutingClockTests.guest_reply
    host_reply = outing_fixture.OutingClockTests.host_reply
    start_trip = outing_fixture.OutingClockTests.start_trip

    async def asyncSetUp(self):
        await outing_fixture.OutingClockTests.asyncSetUp(self)

    async def test_lost_reply_to_old_request_is_replayed_before_the_new_same_second_request(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        before = copy.deepcopy(host["outing"])
        calls = (len(self.a.llm.calls), len(self.b.llm.calls))
        original = self.a.service.transport.request
        delivered = []

        async def lose_first_reply(url, body):
            response = await original(url, body)
            request = body["payload"].get("human_request")
            if request:
                delivered.append(copy.deepcopy(request))
                if len(delivered) == 1:
                    raise ValueError("签名请求已送达，测试丢弃第一次答复")
            return response

        self.a.service.transport.request = lose_first_reply
        await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="先去花园看看")
        self.assertTrue(guest.get("outbox"))
        old = copy.deepcopy(guest["outing_requests"][guest["guest_id"]])
        await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="我又想到先去集市买点心")
        latest = guest["outing_requests"][guest["guest_id"]]
        self.assertEqual(latest["seq"], old["seq"] + 1)
        self.assertEqual(latest["at"], old["at"])
        self.assertEqual(host["outing_requests"], guest["outing_requests"])
        self.assertEqual([request["id"] for request in delivered], [old["id"], old["id"], latest["id"]])
        self.assertFalse(guest.get("manual_continuation"))
        self.assertFalse(guest.get("outbox"))
        self.assertEqual(host["outing"], before)
        self.assertEqual(guest["outing"], before)
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), calls)
