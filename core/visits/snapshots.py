"""Validate public journey snapshots before committing any reply fields."""
from __future__ import annotations

import math
from .protocol import RESERVED, TERMINAL


def validate_snapshot(visit: dict, remote: dict, *, now: float | None = None) -> None:
    if not isinstance(remote, dict) or remote.get("id") != visit["id"]:
        raise ValueError("无效旅程快照")
    actors = {visit["host_id"], visit["guest_id"]}
    if any(remote.get(key) != visit[key] for key in ("host_id", "guest_id")):
        raise ValueError("旅程参与者不一致")
    status = remote.get("status")
    if not isinstance(status, str) or status not in RESERVED | TERMINAL:
        raise ValueError("无效旅程状态")
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
                or any(type(count) is not int or not 0 <= count <= visit["turn_limit"]
                       for count in turns.values())):
            raise ValueError("旅程回合数量无效")
        if visit["role"] == "guest" and any(turns[actor] < visit["turns"][actor] for actor in actors):
            raise ValueError("已确认的回合不能回退")
    if "calls" in remote:
        calls = remote["calls"]
        if not isinstance(calls, dict) or any(actor not in actors or type(count) is not int
                or not 0 <= count <= visit["max_calls"] // 2 +
                    (visit["max_calls"] % 2 if actor == visit["host_id"] else 0)
                for actor, count in calls.items()):
            raise ValueError("旅程调用额度无效")
    if "log" in remote:
        logs = remote["log"]
        if not isinstance(logs, list) or len(logs) > 40:
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
    if (visit["role"] == "guest" and status not in {"inviting", "offered"}
            or not visit.get("arrived_at") and status in TERMINAL):
        logs = remote.get("log", [])
        prior = visit["log"]
        if len(logs) < len(prior) or logs[:len(prior)] != prior:
            raise ValueError("已确认的经过不能被改写")
