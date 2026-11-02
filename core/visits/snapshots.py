"""Validate public journey snapshots before committing any reply fields."""
from __future__ import annotations

import math
import copy
from .protocol import RESERVED, TERMINAL, clean_outing_offer, validate_outing_request_maps
from .outings import CHECKPOINT_LEASE_SECONDS, outing_enabled, valid_activity
from .history_pages import extended_history, expand_snapshot


def validate_snapshot(visit: dict, remote: dict, *, now: float | None = None) -> dict:
    if not isinstance(remote, dict) or remote.get("id") != visit["id"]:
        raise ValueError("无效旅程快照")
    remote = expand_snapshot(visit, remote)
    actors = {visit["host_id"], visit["guest_id"]}
    if any(remote.get(key) != visit[key] for key in ("host_id", "guest_id")):
        raise ValueError("旅程参与者不一致")
    status = remote.get("status")
    if not isinstance(status, str) or status not in RESERVED | TERMINAL:
        raise ValueError("无效旅程状态")
    if "manual" in remote and (type(remote["manual"]) is not bool or remote["manual"] != bool(visit.get("manual"))):
        raise ValueError("人工安排标记不一致")
    if outing_enabled(visit) and (type(remote.get("outing_version")) is not int or remote["outing_version"] != 1):
        raise ValueError("共同活动版本不一致")
    for name, lower in (("max_calls", 2), ("turn_limit", 1)):
        if name in remote and (type(remote[name]) is not int or not lower <= remote[name] <= visit[name]):
            raise ValueError("旅程预算无效")
    for name in ("intro", "end_reason"):
        if name in remote and (not isinstance(remote[name], str) or len(remote[name]) > 500):
            raise ValueError("旅程说明无效")
    for name in ("created_at", "arrived_at", "expires_at", "next_at", "ended_at"):
        if name in remote:
            value = remote[name]
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("旅程时间无效")
    if (remote.get("arrived_at") and not visit.get("arrived_at")
            and (visit["status"] in TERMINAL or now is not None and visit["expires_at"] <= now)):
        raise ValueError("已经结束或到期的邀请不能再确认到访")
    if status in {"active", "finishing"} and not remote.get("arrived_at"):
        raise ValueError("旅程尚未确认到访")
    if "next_actor" in remote and (not isinstance(remote["next_actor"], str)
                                   or remote["next_actor"] not in actors):
        raise ValueError("旅程回合来源无效")
    if "event_stage" in remote and (not isinstance(remote["event_stage"], str)
            or remote["event_stage"] not in {"none", "offered", "skipped", "resolved"}):
        raise ValueError("旅程事件阶段无效")
    if "scene" in remote:
        scene = remote["scene"]
        if not isinstance(scene, dict) or any(not isinstance(scene.get(name, ""), str)
                or len(scene.get(name, "")) > limit
                for name, limit in (("place", 80), ("activity", 120), ("event", 240))):
            raise ValueError("旅程场景无效")
    if "turns" in remote:
        turns = remote["turns"]
        if (not isinstance(turns, dict) or set(turns) != actors
                or any(type(count) is not int or count < 0 or
                       not visit.get("manual") and not outing_enabled(visit) and count > visit["turn_limit"]
                       for count in turns.values())):
            raise ValueError("旅程回合数量无效")
        if visit["role"] == "guest" and any(turns[actor] < visit["turns"][actor] for actor in actors):
            raise ValueError("已确认的回合不能回退")
    if "calls" in remote:
        calls = remote["calls"]
        if not isinstance(calls, dict) or any(actor not in actors or type(count) is not int
                or count < 0 or not visit.get("manual") and not outing_enabled(visit)
                    and count > visit["max_calls"] // 2 + (visit["max_calls"] % 2 if actor == visit["host_id"] else 0)
                for actor, count in calls.items()):
            raise ValueError("旅程调用额度无效")
    if "log" in remote:
        logs = remote["log"]
        if not isinstance(logs, list) or not extended_history(visit) and len(logs) > 40:
            raise ValueError("旅程经过过长")
        for index, row in enumerate(logs):
            if (not isinstance(row, dict) or type(row.get("seq")) is not int or row["seq"] != index + 1
                    or not isinstance(row.get("actor_id"), str) or row["actor_id"] not in actors
                    or not isinstance(row.get("kind"), str) or not isinstance(row.get("text"), str)
                    or len(row["kind"]) > 40 or len(row["text"]) > 500):
                raise ValueError("旅程经过来源无效")
            if "at" in row and (type(row["at"]) not in {int, float}
                                or not math.isfinite(row["at"]) or row["at"] < 0):
                raise ValueError("旅程经过时间无效")
    if "outing" in remote:
        if not outing_enabled(visit) or not valid_activity(remote["outing"], expires_at=remote.get("expires_at", visit["expires_at"]), now=now):
            raise ValueError("共同活动时间或路线无效")
        prior_route = (visit.get("outing") or {}).get("route", [])
        route = remote["outing"]["route"]
        if visit["role"] == "guest" and (len(route) < len(prior_route) or route[:len(prior_route)] != prior_route):
            raise ValueError("已确认路线不能被改写")
        if visit["role"] == "host" and (len(route) > len(prior_route) or route != prior_route[:len(route)]):
            raise ValueError("访客不能替东道主推进共同路线")
        outing = remote["outing"]
        if outing.get("return_agreed") and set(outing.get("farewells", {})) != actors:
            raise ValueError("共同归家必须保留双方独立原话")
        prior = visit.get("outing") or {}
        if (visit["role"] == "guest" and prior.get("stage") == outing.get("stage")
                and "pause_choices" in prior and "pause_choices" not in outing):
            raise ValueError("不能删除已确认的休息意愿")
        if "pause_choices" in outing:
            pauses = outing["pause_choices"]
            first, cadence = outing.get("pause_at"), outing.get("cadence_seconds")
            if (not isinstance(pauses, dict) or set(pauses) != actors
                    or any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 240
                           for value in pauses.values())
                    or type(first) not in {int, float} or not math.isfinite(first)
                    or not outing["started_at"] <= first < outing["until"]
                    or now is not None and first > now + 300
                    or type(cadence) not in {int, float} or not 1 <= cadence <= 180
                    or outing.get("quiet_until") != min(outing["until"], first + max(cadence, *(value * 60 for value in pauses.values())))):
                raise ValueError("共同休息节奏无效")
            if visit["role"] == "guest":
                same_checkpoint = (len(route) == len(prior_route) and "pause_choices" in prior
                                   and prior.get("pause_at") == first)
                if same_checkpoint and any(prior.get(key) != outing.get(key) for key in
                                           ("pause_choices", "cadence_seconds", "quiet_until")):
                    raise ValueError("已经共同确认的休息意愿不能被改写")
                if (not same_checkpoint and len(route) == len(prior_route)
                        and not _submitted_pause_choice(visit, remote)):
                    raise ValueError("共同休息缺少本次访客独立回应证明")
                expected = (prior["pause_choices"][visit["guest_id"]] if same_checkpoint
                            else (visit.get("decision") or {}).get("pause_minutes", 5))
                if pauses[visit["guest_id"]] != expected:
                    raise ValueError("不能替访客编造休息意愿")
        if visit["role"] == "guest" and len(route) > len(prior_route):
            offer = visit.get("outing_offer") or {}
            choice = visit.get("decision") or {}
            stop = route[-1]
            if (len(route) != len(prior_route) + 1 or choice.get("outing_accept") is not True
                    or choice.get("leave") is True or not offer
                    or any(stop.get(key) != offer.get(key) for key in ("place", "activity"))
                    or stop["until"] != stop["started_at"] + offer["minutes"] * 60):
                raise ValueError("共同活动缺少访客独立接受证明")
            agreed = offer.get("return_after") is True and choice.get("return_after") is True
            if outing.get("return_agreed", False) != agreed or (agreed
                    and outing.get("farewells", {}).get(visit["guest_id"]) != choice.get("farewell", "")):
                raise ValueError("共同归家缺少访客本人原话")
        if prior and prior.get("stage") == outing.get("stage") and any(
                prior.get(key) != outing.get(key) for key in ("return_agreed", "farewells")):
            raise ValueError("已确认的归家安排不能被改写")
    if "outing_offer" in remote:
        offer = remote["outing_offer"]
        if (not outing_enabled(visit) or not isinstance(offer, dict)
                or clean_outing_offer(offer) != {**offer, "return_after": offer.get("return_after", False),
                                                "pause_minutes": offer.get("pause_minutes", 5)}):
            raise ValueError("共同活动提议无效")
    remote = _merge_outing_requests(visit, remote, actors, now=now)
    holds = remote.get("budget_holds", {})
    if not isinstance(holds, dict) or not set(holds) <= actors:
        raise ValueError("自动相处等待来源无效")
    for hold in holds.values():
        if (not isinstance(hold, dict) or not isinstance(hold.get("reason", ""), str)
                or len(hold.get("reason", "")) > 240 or type(hold.get("reset_at")) not in {int, float}
                or not math.isfinite(hold["reset_at"]) or hold["reset_at"] < 0
                or now is not None and hold["reset_at"] > now + 172800):
            raise ValueError("自动相处等待时间无效")
    if visit["role"] == "guest" and holds.get(visit["guest_id"]) != visit.get("budget_holds", {}).get(visit["guest_id"]):
        raise ValueError("东道主不能替访客编造额度等待")
    expires = remote.get("expires_at", visit["expires_at"])
    if outing_enabled(visit) and visit.get("arrived_at") and expires > visit["expires_at"]:
        basis = max([(remote.get("outing") or {}).get("until", 0)]
                    + [hold["reset_at"] for hold in holds.values()])
        if expires > basis + CHECKPOINT_LEASE_SECONDS:
            raise ValueError("联络期限延长缺少共同活动或预算等待依据")
    if (visit["role"] == "guest" and status not in {"inviting", "offered"}
            or not visit.get("arrived_at") and status in TERMINAL):
        logs = remote.get("log", [])
        prior = visit["log"]
        if len(logs) < len(prior) or logs[:len(prior)] != prior:
            raise ValueError("已确认的经过不能被改写")
    return remote


def _submitted_pause_choice(visit: dict, remote: dict) -> bool:
    """Bind a new pause to the visitor's durable, submitted turn, not an old choice."""
    cursor = visit.get("decision_cursor") or {}
    prior = visit.get("outing") or {}
    guest = visit["guest_id"]
    # A human suggestion may arrive after this turn was signed. Its request map
    # belongs to the next discussion and does not invalidate the submitted reply.
    return (visit.get("unsent_turn") is True and isinstance(visit.get("decision"), dict)
            and visit["decision"].get("leave") is not True
            and cursor.get("phase") == "turn" and cursor.get("status") == visit["status"] == "active"
            and cursor.get("log_seq") == len(visit["log"]) and cursor.get("turns") == visit["turns"]
            and cursor.get("next_actor") == visit["next_actor"] == guest
            and cursor.get("outing_stage") == prior.get("stage")
            and cursor.get("quiet_until") == prior.get("quiet_until", 0)
            and remote.get("turns", {}).get(guest) == visit["turns"][guest] + 1)


def _merge_outing_requests(visit: dict, remote: dict, actors: set[str], *, now: float | None) -> dict:
    requests, handled, turn = validate_outing_request_maps(remote, actors, now=now)
    prior_requests, prior_handled, _ = validate_outing_request_maps(visit, actors)
    if (requests or handled or turn) and (not outing_enabled(visit) or not remote.get("manual")
                                         or not remote.get("arrived_at")):
        raise ValueError("真人游玩请求缺少已到访的人工相处证明")
    local_id = visit["host_id"] if visit["role"] == "host" else visit["guest_id"]
    merged = copy.deepcopy(prior_requests)
    for actor, request in requests.items():
        prior = prior_requests.get(actor)
        if actor == local_id:
            if (not prior or request["seq"] > prior["seq"]
                    or request["seq"] == prior["seq"] and request != prior):
                raise ValueError("不能替本人改写真人游玩请求")
        elif prior and request["seq"] == prior["seq"] and request != prior:
            raise ValueError("已经确认的真人游玩请求不能被改写")
        elif not prior or request["seq"] > prior["seq"]:
            merged[actor] = request
    if visit["role"] == "host":
        return remote  # The host receives only the sender's separately signed request.
    merged_handled = dict(prior_handled)
    cursor_turn = (visit.get("decision_cursor") or {}).get("outing_request_turn") or {}
    for actor, request_id in handled.items():
        if request_id == prior_handled.get(actor):
            continue
        current = merged.get(actor)
        if not current or current["id"] != request_id:
            continue  # An older acknowledgement cannot consume a newer request.
        if not visit.get("unsent_turn") or cursor_turn.get(actor) != request_id:
            raise ValueError("真人游玩请求缺少本人独立回应证明")
        merged_handled[actor] = request_id
    result = copy.deepcopy(remote)
    if merged or handled or turn or prior_handled:
        result.update(outing_requests=merged, outing_requests_handled=merged_handled,
                      outing_request_turn=turn)
    return result
