"""Keep speech provenance across queued actions and mixed tick outcomes."""

import functools
import inspect
from contextvars import ContextVar
from contextlib import contextmanager


SILENT_FOLLOWUP = ContextVar("world_silent_followup", default=False)
SEND_CHECK = ContextVar("world_send_check", default=None)


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
                kind = bound["action"].get("speech_kind", previous)
            elif source == "sleep":
                kind = (bound["state"].drowsy_sleep_step or {}).get("speech_kind", "")
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
                kind = step.get("kind") or ("schedule" if plan.get("source") == "schedule" else "")
            elif source == "actions":
                kind = "schedule" if bound.get("from_schedule") else previous
                if not kind and not bound.get("autonomous", True):
                    kind = "reply"
            else:
                kind = source
            if source == "completion":
                action = bound["action"]
                outcome.contact_id = action.get("contact_id") or action.get("plan_id") or outcome.contact_id
                outcome.contact_people.update(action.get("contact_people") or [])
            outcome.speech_kind = kind
            try:
                return await function(self, *args, **kwargs)
            finally:
                outcome.speech_kind = previous

        return invoke
    return decorate
