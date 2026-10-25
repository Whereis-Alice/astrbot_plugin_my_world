"""Detach message/request data without cloning live pipeline or tool handles."""
from __future__ import annotations

import copy
from typing import Any

from ..core.constants import MW_TURN_EXTRA

TRANSIENT_EXTRAS = frozenset({
    "provider_request", "llm_checkpoint_id", "selected_model", MW_TURN_EXTRA,
    "_clean_ltm_session", "_clean_group_context_session",
})


def snapshot_event(event: Any) -> Any:
    snapshot = copy.copy(event)  # Transport belongs to the adapter.
    snapshot.message_obj = copy.deepcopy(event.message_obj)
    for name in ("_extras", "_extra", "extra"):
        original = getattr(event, name, None)
        if not isinstance(original, dict):
            continue
        detached = {}
        for key, value in original.items():
            if key in TRANSIENT_EXTRAS or str(key).startswith("my_world_"):
                continue
            try:
                detached[key] = copy.deepcopy(value)
            except Exception:
                # An opaque extension handle is not message evidence. Never
                # share it, cancel it or discard all the other plugin data.
                continue
        setattr(snapshot, name, detached)
    return snapshot


def snapshot_request(request: Any) -> Any:
    """Isolate public-hook content; tool implementations retain their live owners."""
    toolset = getattr(request, "func_tool", None)
    memo = {}
    if toolset is not None:
        tools = copy.copy(toolset)
        tools.tools = list(toolset.tools)
        memo[id(toolset)] = tools
    return copy.deepcopy(request, memo)
