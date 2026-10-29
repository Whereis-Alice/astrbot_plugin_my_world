"""Keep speech provenance across queued actions and mixed tick outcomes."""

import functools
import inspect
from contextvars import ContextVar
from contextlib import contextmanager


SILENT_FOLLOWUP = ContextVar("world_silent_followup", default=False)
SEND_CHECK = ContextVar("world_send_check", default=None)
REPLY_TARGET = ContextVar("world_reply_target", default=None)
SPEECH_OUTCOME = ContextVar("world_speech_outcome", default=None)
PLAN_SPEECH = ContextVar("world_plan_speech", default=None)
SEND_RECEIPT = ContextVar("world_send_receipt", default=None)
CONTACT_TARGET = ContextVar("world_contact_target", default=None)


@contextmanager
def contact_send(session_id, people, index=0):
    token = CONTACT_TARGET.set((session_id, tuple(sorted(people)), index))
    try:
        yield
    finally:
        CONTACT_TARGET.reset(token)


def contact_targets(session_id, index=0):
    target = CONTACT_TARGET.get()
    return target[1] if target and target[0] == session_id and target[2] == index else ()


def activity_speech_kind(kind, definition):
    """A personal activity cannot retain permission to contact someone later.

    Also applies to persisted actions from older versions. User-requested,
    scheduled and tool work keep their original provenance.
    """
    if (kind == "outreach" and definition is not None
            and definition.category == "continuous"
            and definition.target_type in {"none", "node"}
            and definition.llm_level not in {"tool", "command"}
            and not definition.tool_list() and not definition.trigger_command):
        return "life_share"
    return kind


@contextmanager
def watch_send(receipt):
    token = SEND_RECEIPT.set(receipt)
    try:
        yield
    finally:
        SEND_RECEIPT.reset(token)


async def accept_segment(session_id: str, text: str, index: int):
    receipt = SEND_RECEIPT.get()
    if receipt is not None:
        await receipt(session_id, text, index)


@contextmanager
def quote_send(session_id: str, message_id: str, index: int = 0):
    token = REPLY_TARGET.set((session_id, message_id, index))
    try:
        yield
    finally:
        REPLY_TARGET.reset(token)


def reply_target(session_id: str, index: int = 0) -> str:
    target = REPLY_TARGET.get()
    return target[1] if target and target[0] == session_id and target[2] == index else ""


@contextmanager
def guard_send(check):
    """Recheck authorization after typing waits, without changing port signatures."""
    token = SEND_CHECK.set(check)
    try:
        yield
    finally:
        SEND_CHECK.reset(token)


def segment_allowed(session_id: str, index: int = 0) -> bool:
    check = SEND_CHECK.get()
    return check is None or bool(check(session_id, index))


def speech_origin(source):
    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def invoke(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs).arguments
            outcome = bound["outcome"]
            previous = outcome.speech_kind
            if source == "completion":
                kind = bound["action"].get("speech_kind") or previous or "life_share"
            elif source == "sleep":
                kind = (bound["state"].drowsy_sleep_step or {}).get("speech_kind") or "life_share"
            elif source == "plan":
                plan = bound["state"].current_plan or {}
                outcome.contact_id = plan.get("contact_id") or plan.get("id") or outcome.contact_id
                outcome.contact_people.update(plan.get("contact_people") or [])
                for candidate in plan.get("steps") or []:
                    definition = self.world.action_map().get(candidate.get("action"))
                    if definition is not None and definition.target_type == "user" and candidate.get("target"):
                        outcome.contact_people.add(str(candidate["target"]))
                steps = plan.get("steps") or []
                index = int(plan.get("current_step") or 0)
                step = steps[index] if 0 <= index < len(steps) else {}
                kind = step.get("kind") or ("schedule" if plan.get("source") == "schedule"
                    else "outreach" if outcome.contact_people else "life_share")
                scene = step.get("speech_scene") or plan.get("speech_scene") or {}
                if kind in {"participation", "browse_reply", "continuation", "interject"} and scene.get("rows"):
                    target = str(scene.get("target") or "")
                    self.claim_chat_scene(outcome, target, scene["rows"], window=scene.get("window", 900), state=bound["state"])
                    captured = outcome.chat_media_scenes[target]["rows"]
                    known = {str(row.get("seq")) for row in captured}
                    # The archive is bounded; a saved source still owns its
                    # sender and watermark even after newer messages evict it.
                    from copy import deepcopy
                    captured.extend(deepcopy(row) for row in scene["rows"] if str(row.get("seq")) not in known)
                    captured.sort(key=lambda row: int(row.get("seq") or 0))
                    if scene.get("reply_to"):
                        outcome.reply_targets[target] = str(scene["reply_to"])
            elif source == "actions":
                kind = "schedule" if bound.get("from_schedule") else previous
                if not kind and not bound.get("autonomous", True):
                    kind = "reply"
                kind = kind or "life_share"
            else:
                kind = source
            if source == "completion":
                action = bound["action"]
                outcome.contact_id = action.get("contact_id") or action.get("plan_id") or outcome.contact_id
                outcome.contact_people.update(action.get("contact_people") or [])
                kind = activity_speech_kind(kind, self.world.action_map().get(action.get("type")))
            elif source == "plan":
                kind = activity_speech_kind(kind, self.world.action_map().get(step.get("action")))
            outcome.speech_kind = kind
            token = SPEECH_OUTCOME.set(outcome)
            plan_token = PLAN_SPEECH.set((outcome, plan) if source == "plan"
                else PLAN_SPEECH.get() if source == "actions" else None)
            try:
                return await function(self, *args, **kwargs)
            finally:
                PLAN_SPEECH.reset(plan_token)
                SPEECH_OUTCOME.reset(token)
                outcome.speech_kind = previous

        return invoke
    return decorate
