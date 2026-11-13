"""Jointly accepted activities with a shared clock, without minute-by-minute inference."""
from __future__ import annotations

import copy
import math

from .protocol import clean_outing_offer
from .activities import begin_shared_activity

OUTING_VERSION = 1
CHECKPOINT_LEASE_SECONDS = 3600


def outing_enabled(visit: dict) -> bool:
    return type(visit.get("outing_version")) is int and visit["outing_version"] == OUTING_VERSION


def pending_outing_requests(visit: dict) -> dict:
    """A signed human suggestion starts a conversation, never changes the shared plan."""
    if not outing_enabled(visit) or (visit.get("outing") or {}).get("return_agreed"):
        return {}
    handled = visit.get("outing_requests_handled", {})
    return {actor: request for actor, request in visit.get("outing_requests", {}).items()
            if handled.get(actor) != request["id"]}


def activity_waiting(visit: dict, now: float) -> bool:
    activity = visit.get("outing") or {}
    quiet_until = activity.get("quiet_until", activity.get("until", 0))
    return bool(outing_enabled(visit) and activity and quiet_until > now and not pending_outing_requests(visit))


def proposed_activity(decision: dict) -> dict:
    """Legacy-shaped model scenes still describe a real choice, not a made-up fallback."""
    offer = clean_outing_offer(decision.get("outing"))
    if offer:
        return offer
    scene = decision.get("scene") or {}
    return clean_outing_offer({**scene, "minutes": 45, "pause_minutes": decision.get("pause_minutes", 5)})


def begin_activity(visit: dict, offer: dict, now: float, guest_choice: dict | None = None,
                   *, floor_seconds: float = 60) -> None:
    """Only the host calls this after receiving the visitor's independent acceptance."""
    route = copy.deepcopy((visit.get("outing") or {}).get("route", []))
    minutes = offer["minutes"]
    travel = float(offer.get("travel_seconds", 0))
    arrival = now + travel
    until = arrival + minutes * 60
    guest_choice = guest_choice or {}
    return_agreed = offer.get("return_after") is True and guest_choice.get("return_after") is True
    farewells = {visit["host_id"]: visit.get("outing_host_farewell", ""),
                 visit["guest_id"]: guest_choice.get("farewell", "")} if return_agreed else {}
    stop = {"place": offer["place"], "activity": offer["activity"], "started_at": arrival, "until": until}
    if travel:
        previous = visit.get("outing") or {}
        origin = (previous.get("origin_place") if previous.get("started_at", 0) > now else previous.get("place"))
        origin = origin or (visit.get("scene") or {}).get("place") or (visit.get("local") or {}).get("place", "")
        stop.update(travel_seconds=travel, departed_at=now, travel_until=arrival, origin_place=origin)
    route.append(stop)
    visit["outing"] = {"version": OUTING_VERSION, "stage": len(route) - 1,
                       **stop, "route": route, "return_agreed": return_agreed, "farewells": farewells}
    visit["expires_at"] = max(visit["expires_at"], until + CHECKPOINT_LEASE_SECONDS)
    visit["scene"] = {"place": offer["place"], "activity": offer["activity"], "event": offer.get("event", "")}
    begin_shared_activity(visit)
    visit.pop("outing_offer", None)
    pause_activity(visit, offer.get("pause_minutes", 5), guest_choice.get("pause_minutes", 5), now,
                   floor_seconds=floor_seconds)


def pause_activity(visit: dict, host_minutes: float, guest_minutes: float, now: float,
                   *, floor_seconds: float = 60) -> None:
    """Both characters have independently expressed how long they want quiet company."""
    activity = visit["outing"]
    cadence = max(1, min(180, floor_seconds))
    base = min(activity["until"], max(now, activity["started_at"]))
    quiet_until = min(activity["until"], base + max(cadence, host_minutes * 60, guest_minutes * 60))
    activity["quiet_until"] = quiet_until
    activity["pause_choices"] = {visit["host_id"]: host_minutes, visit["guest_id"]: guest_minutes}
    activity["pause_at"], activity["cadence_seconds"] = base, cadence
    visit["next_actor"] = visit["host_id"]
    visit["next_at"] = quiet_until


def valid_activity(activity: object, *, expires_at: float, now: float | None = None) -> bool:
    if not isinstance(activity, dict) or activity.get("version") != OUTING_VERSION:
        return False
    route = activity.get("route")
    stage = activity.get("stage")
    if (not isinstance(route, list) or not route
            or type(stage) is not int or stage != len(route) - 1):
        return False
    previous = 0.
    for stop in route:
        if not isinstance(stop, dict) or not clean_outing_offer({**stop, "minutes": 45}):
            return False
        first, last = stop.get("started_at"), stop.get("until")
        if (any(type(value) not in {int, float} or not math.isfinite(value) for value in (first, last))
                or first <= 0 or last <= first or last > expires_at or first < previous):
            return False
        travel = stop.get("travel_seconds", 0)
        if type(travel) not in {int, float} or not math.isfinite(travel) or not 0 <= travel <= 86400:
            return False
        if travel:
            departed = stop.get("departed_at")
            if (type(departed) not in {int, float} or not math.isfinite(departed)
                    or departed <= 0 or departed > first
                    or not math.isclose(first - departed, travel, abs_tol=0.000001)
                    or stop.get("travel_until") != first
                    or not isinstance(stop.get("origin_place"), str) or len(stop["origin_place"]) > 80
                    or now is not None and departed > now + 300):
                return False
        elif now is not None and first > now + 300:
            return False
        previous = first
    if type(activity.get("return_agreed", False)) is not bool:
        return False
    quiet = activity.get("quiet_until", activity["until"])
    if (type(quiet) not in {int, float} or not math.isfinite(quiet)
            or not activity["started_at"] <= quiet <= activity["until"]):
        return False
    farewells = activity.get("farewells", {})
    if not isinstance(farewells, dict) or any(not isinstance(value, str) or len(value) > 100
                                           for value in farewells.values()):
        return False
    return all(activity.get(key) == route[-1].get(key) for key in (
        "place", "activity", "started_at", "until", "travel_seconds", "departed_at", "travel_until", "origin_place"))
