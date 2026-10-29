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
def contact_send(session_id, people, index=0, *, positions=None):
    targets = ({index: tuple(sorted(people))} if positions is None else
               {i: tuple(sorted(values)) for i, values in positions.items()})
    token = CONTACT_TARGET.set((session_id, targets))
    try:
        yield
    finally:
        CONTACT_TARGET.reset(token)


def contact_targets(session_id, index=0):
    target = CONTACT_TARGET.get()
    return target[1].get(index, ()) if target and target[0] == session_id else ()


def plan_contact_people(outcome):
    """The plan's default recipient survives queuing; an action target does not."""
    context = PLAN_SPEECH.get()
    if context and context[0] is outcome:
        return set(context[1].get("contact_people") or [])
    return set(outcome.contact_people)


def private_recipient(session_id):
    parts = str(session_id or "").split(":", 2)
    return parts[2] if len(parts) == 3 and parts[1] in {"FriendMessage", "PrivateMessage"} else ""


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
            previous_people = outcome.contact_people
            previous_home = outcome.speech_home
            if source == "completion":
                kind = bound["action"].get("speech_kind") or previous or "life_share"
            elif source == "sleep":
                kind = (bound["state"].drowsy_sleep_step or {}).get("speech_kind") or "life_share"
            elif source == "plan":
                plan = bound["state"].current_plan or {}
                outcome.contact_id = plan.get("contact_id") or plan.get("id") or outcome.contact_id
                steps = plan.get("steps") or []
                index = int(plan.get("current_step") or 0)
                step = steps[index] if 0 <= index < len(steps) else {}
                outcome.contact_people = set(plan.get("contact_people") or [])
                definition = self.world.action_map().get(step.get("action"))
                if definition is not None and definition.target_type == "user" and step.get("target"):
                    outcome.contact_people = {str(step["target"])}
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
            elif source == "action":
                kind = previous
                definition, action = bound["definition"], bound["action"]
                if definition.target_type == "user":
                    target = self._speech_target(bound["state"], action, outcome)
                    if target is not None:
                        outcome.speech_home = target
                        recipient = private_recipient(target)
                        if not action.target and recipient:
                            action.target = recipient
                    if action.target:
                        outcome.contact_people = {str(action.target)}
                        if kind in {"", "life_share"}:
                            kind = "outreach"
            else:
                kind = source
            if source == "completion":
                action = bound["action"]
                outcome.contact_id = action.get("contact_id") or action.get("plan_id") or outcome.contact_id
                outcome.contact_people = set(action.get("contact_people") or [])
                definition = self.world.action_map().get(action.get("type"))
                if definition is not None and definition.target_type == "user":
                    recipient = private_recipient(action.get("session") or outcome.home())
                    if recipient and action.get("target") and str(action["target"]) != recipient:
                        kind = "quiet"
                        outcome.contact_people = set()
                        outcome.notes.append("私聊收件人与动作对象冲突，动作照常结算，取消对外续说")
                    elif recipient or action.get("target"):
                        outcome.contact_people = {recipient or str(action["target"])}
                kind = activity_speech_kind(kind, definition)
            elif source == "plan":
                kind = activity_speech_kind(kind, self.world.action_map().get(step.get("action")))
            outcome.speech_kind = kind
            token = SPEECH_OUTCOME.set(outcome)
            plan_token = PLAN_SPEECH.set((outcome, plan) if source == "plan"
                else PLAN_SPEECH.get() if source in {"actions", "action"} else None)
            try:
                return await function(self, *args, **kwargs)
            finally:
                PLAN_SPEECH.reset(plan_token)
                SPEECH_OUTCOME.reset(token)
                outcome.speech_kind = previous
                if source in {"plan", "completion", "action"}:
                    outcome.contact_people = previous_people
                if source in {"action", "completion"}:
                    outcome.speech_home = previous_home

        return invoke
    return decorate
