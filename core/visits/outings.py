"""Jointly accepted activities with a shared clock, without minute-by-minute inference."""
from __future__ import annotations

import copy
import math

from .protocol import ID, clean_outing_offer
from .activities import begin_shared_activity

OUTING_VERSION = 1
CHECKPOINT_LEASE_SECONDS = 3600
CONTACT_LEASE_VERSION = 1
QUIET_CLOCK_VERSION = 2
PROGRESS_LOG_KINDS = frozenset({"say", "action_plan", "action_control", "milestone", "experience", "result"})


def outing_enabled(visit: dict) -> bool:
    return type(visit.get("outing_version")) is int and visit["outing_version"] == OUTING_VERSION


def validate_contact_lease_version(value: object) -> None:
    if type(value) is not int or value != CONTACT_LEASE_VERSION:
        raise ValueError("联络期限能力版本无效")


def validate_quiet_clock_version(value: object) -> None:
    if type(value) is not int or value != QUIET_CLOCK_VERSION:
        raise ValueError("共同静处时钟能力版本无效")


def quiet_clock_enabled(visit: dict) -> bool:
    return outing_enabled(visit) and visit.get("quiet_clock_supported") is True


def quiet_deadline(visit: dict, first: float, cadence: float, pauses: dict) -> float:
    """Keep chosen attention inside the existing contact or agreed return window."""
    activity = visit["outing"]
    limit = (activity["until"] if activity.get("return_agreed")
             else activity.get("quiet_contact_until", visit["expires_at"]))
    return min(limit, first + max(cadence, *(value * 60 for value in pauses.values())))


def renew_contact_lease(visit: dict, now: float) -> bool:
    """Confirmed interaction gets a finite contact window, never a new return time."""
    expiry = visit.get("expires_at")
    if (not outing_enabled(visit) or visit.get("status") != "active" or not visit.get("arrived_at")
            or visit.get("contact_lease_supported") is not True
            or (visit.get("outing") or {}).get("return_agreed")
            or any(type(value) not in {int, float} or not math.isfinite(value) for value in (now, expiry))
            or expiry <= now):
        return False
    renewed = max(expiry, now + CHECKPOINT_LEASE_SECONDS)
    if renewed == expiry:
        return False
    visit["expires_at"] = renewed
    return True


def confirmed_progress_lease_limit(visit: dict, remote: dict, *, now: float | None = None) -> float:
    """Bound signed catch-up by new actual facts linked before each contact expiry."""
    expiry = visit.get("expires_at", 0)
    if type(expiry) not in {int, float} or not math.isfinite(expiry):
        return 0
    if type(now) not in {int, float} or not math.isfinite(now):
        return expiry  # No clock proof means no extension based on new facts.
    if (not outing_enabled(visit) or not visit.get("arrived_at")
            or type(remote.get("contact_lease_version")) is not int
            or remote.get("contact_lease_version") != CONTACT_LEASE_VERSION
            or remote.get("status") not in {"active", "finishing", "completed", "interrupted", "expired"}
            or (visit.get("outing") or {}).get("return_agreed")
            or (remote.get("outing") or {}).get("return_agreed")):
        return expiry
    previous, logs = visit.get("log", []), remote.get("log", [])
    if not isinstance(logs, list) or len(logs) <= len(previous) or logs[:len(previous)] != previous:
        return expiry
    actors = {visit.get("host_id"), visit.get("guest_id")}
    arrived = visit.get("arrived_at", 0)
    stamps = []
    for row in logs[len(previous):]:
        if (not isinstance(row, dict) or row.get("actor_id") not in actors
                or row.get("kind") not in PROGRESS_LOG_KINDS
                or not isinstance(row.get("text"), str) or not row["text"].strip()):
            continue
        at = row.get("at")
        if (type(at) not in {int, float} or not math.isfinite(at) or at < arrived
                or at > now):
            continue
        stamps.append(at)
    for at in sorted(stamps):
        if at >= expiry:
            break  # A fact first made after losing contact cannot revive the visit.
        expiry = max(expiry, at + CHECKPOINT_LEASE_SECONDS)
    return expiry


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


def activity_offer(visit: dict, decision: dict, now: float) -> dict:
    """Use the same supported proposal shapes for planning and execution checks."""
    if decision.get("continue_activity") and visit.get("outing"):
        return {}
    return proposed_activity(decision)


def valid_quiet_action_ids(value: object) -> bool:
    """A bounded acknowledgement of seen clocks, never a result settlement."""
    return bool(isinstance(value, list) and len(value) <= 12
                and all(isinstance(item, str) and len(item) <= 80 and ID.fullmatch(item) for item in value)
                and len(set(value)) == len(value))


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
                   *, floor_seconds: float = 60, quiet_action_ids: list[str] | None = None) -> None:
    """Both characters have independently expressed how long they want quiet company."""
    activity = visit["outing"]
    cadence = max(1, min(180, floor_seconds))
    activity["pause_choices"] = {visit["host_id"]: host_minutes, visit["guest_id"]: guest_minutes}
    continued = (quiet_clock_enabled(visit) and not activity.get("return_agreed")
                 and now >= activity["until"])
    base = max(now, activity["started_at"]) if continued else min(activity["until"], max(now, activity["started_at"]))
    if continued:
        activity["quiet_clock_version"] = QUIET_CLOCK_VERSION
        activity["quiet_contact_until"] = visit["expires_at"]
        quiet_until = quiet_deadline(visit, base, cadence, activity["pause_choices"])
    else:
        activity.pop("quiet_clock_version", None)
        activity.pop("quiet_contact_until", None)
        quiet_until = min(activity["until"], base + max(cadence, host_minutes * 60, guest_minutes * 60))
    activity["quiet_until"] = quiet_until
    activity["pause_at"], activity["cadence_seconds"] = base, cadence
    if quiet_action_ids:
        activity["quiet_action_ids"] = list(quiet_action_ids)
    else:
        activity.pop("quiet_action_ids", None)
    visit["next_actor"] = visit["host_id"]
    visit["next_at"] = quiet_until


def valid_activity(activity: object, *, expires_at: float, now: float | None = None,
                   quiet_clock_supported: bool = False) -> bool:
    if not isinstance(activity, dict) or activity.get("version") != OUTING_VERSION:
        return False
    route = activity.get("route")
    stage = activity.get("stage")
    if (not isinstance(route, list) or not route
            or type(stage) is not int or stage != len(route) - 1):
        return False
    previous = 0.
    previous_ended = False
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
                    or departed <= 0 or departed > first or previous_ended and departed < previous
                    or not math.isclose(first - departed, travel, abs_tol=0.000001)
                    or stop.get("travel_until") != first
                    or not isinstance(stop.get("origin_place"), str) or len(stop["origin_place"]) > 80
                    or now is not None and departed > now + 300):
                return False
        elif now is not None and first > now + 300:
            return False
        if "ended_at" in stop:
            ended = stop["ended_at"]
            if (type(ended) not in {int, float} or not math.isfinite(ended)
                    or ended < stop.get("departed_at", first)
                    or now is not None and ended > now + 300):
                return False
            previous = ended  # A changed destination may end travel before its planned arrival.
            previous_ended = True
        else:
            previous = first
            previous_ended = False
    if type(activity.get("return_agreed", False)) is not bool:
        return False
    continued = "quiet_clock_version" in activity
    if continued and (type(activity["quiet_clock_version"]) is not int
                      or activity["quiet_clock_version"] != QUIET_CLOCK_VERSION
                      or not quiet_clock_supported or activity.get("return_agreed")
                      or "pause_choices" not in activity):
        return False
    if continued:
        contact_until = activity.get("quiet_contact_until")
        if (type(contact_until) not in {int, float} or not math.isfinite(contact_until)
                or not activity["until"] < contact_until <= expires_at):
            return False
    elif "quiet_contact_until" in activity:
        return False
    quiet = activity.get("quiet_until", activity["until"])
    if (type(quiet) not in {int, float} or not math.isfinite(quiet)
            or not activity["started_at"] <= quiet <= (activity["quiet_contact_until"] if continued else activity["until"])):
        return False
    if "quiet_action_ids" in activity:
        if not valid_quiet_action_ids(activity["quiet_action_ids"]) or "pause_choices" not in activity:
            return False
    farewells = activity.get("farewells", {})
    if not isinstance(farewells, dict) or any(not isinstance(value, str) or len(value) > 100
                                           for value in farewells.values()):
        return False
    return all(activity.get(key) == route[-1].get(key) for key in (
        "place", "activity", "started_at", "until", "travel_seconds", "departed_at", "travel_until", "origin_place"))
