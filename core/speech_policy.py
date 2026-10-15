"""Keep speech provenance across queued actions and mixed tick outcomes."""

import functools
import inspect


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
            outcome.speech_kind = kind
            try:
                return await function(self, *args, **kwargs)
            finally:
                outcome.speech_kind = previous

        return invoke
    return decorate
