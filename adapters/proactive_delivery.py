"""Run response/decorating hooks on public speech, preserving delivery identity."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from .proactive_context import isolated_event, source_event
from .proactive_hooks import fire_hook as _fire_hook

WIRE = ContextVar("my_world_proactive_wire", default=None)


async def fire_hook(plugin, *args, **kwargs):
    from ..core.engine.life_inference import released_life_lock
    async with released_life_lock(plugin.engine):
        return await _fire_hook(plugin, *args, **kwargs)


@contextmanager
def segment(target, index):
    row = (WIRE.get() or {}).get((target, index))
    token = WIRE.set({(target, 0): row} if row else {})
    try:
        yield
    finally:
        WIRE.reset(token)


def prepared_chain(target, text, index):
    wire = WIRE.get() or {}
    row = wire.get((target, index))
    return list(row["chain"]) if row and row["text"] == text else None


@asynccontextmanager
async def delivery(plugin, target, view, *, echo_count=0, instant=False):
    original = view.native_events.get(target) or source_event(plugin, target)
    if (original is None or not hasattr(original, "message_obj")
            or not callable(getattr(plugin.context, "tool_loop_agent", None))):
        yield
        return
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
    from astrbot.core.provider.entities import LLMResponse
    messages, kinds, identities, wire = [], [], [], {}
    source_kinds, source_ids = view.speech_kinds.get("", []), view.speech_ids.get("", [])
    debug_positions = list(view.debug_positions)
    positions = {0: 0}
    for index, text in enumerate(view.messages):
        kind = source_kinds[index] if index < len(source_kinds) else view.speech_kind
        identity = source_ids[index] if index < len(source_ids) else ""
        if kind == "reply":
            messages.append(text)
            kinds.append(kind)
            identities.append(identity)
            positions[index + 1] = len(messages)
            continue
        event = isolated_event(original, plugin.context, target)
        request = original.get_extra("provider_request")
        if request is not None:
            event.set_extra("provider_request", copy.deepcopy(request))
        # A life share addresses the room, not the sender of its last message.
        if ":GroupMessage:" in target and kind not in {"participation", "browse_reply", "continuation", "interject"}:
            sender = getattr(event.message_obj, "sender", None)
            if sender is not None:
                person = next(iter(sorted(view.contact_people)), "") if kind == "outreach" else ""
                sender.user_id = person or event.get_group_id()
                sender.nickname = person or "群里"
        raw = plugin.take_llm_response(target)
        response = copy.copy(raw) if raw is not None else LLMResponse(role="assistant", completion_text=text)
        response.completion_text = text
        response.reasoning_content = ""
        response.result_chain = None
        if await fire_hook(plugin, event, "OnLLMResponseEvent", response):
            positions[index + 1] = len(messages)
            continue
        edited = str(response.completion_text or "").strip()
        chain = getattr(response.result_chain, "chain", None)
        if chain is None and isinstance(response.result_chain, list):
            chain = response.result_chain
        chain = list(chain) if chain else [Plain(text=edited)] if edited else []
        result = MessageEventResult(chain=chain)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)
        if await fire_hook(plugin, event, "OnDecoratingResultEvent"):
            positions[index + 1] = len(messages)
            continue
        current = event.get_result()
        chain = list(getattr(current, "chain", []) or [])
        body = "\n".join(part.text for part in chain if isinstance(part, Plain) and part.text).strip()
        if not chain:
            positions[index + 1] = len(messages)
            continue
        # A voice-only replacement still speaks the text; retain it for the
        # character's memory, while the transport sends only the actual chain.
        body = body or edited or str(text)
        position = len(messages)
        messages.append(body)
        kinds.append(kind)
        identities.append(identity)
        wire[(target, position)] = {"text": body, "chain": chain, "event": event, "accepted": False}
        trace = next((r for r in view.speech_decisions if r["id"] == identity), None)
        if trace and body != text:
            drafts = trace.get("drafts", [])
            if text in drafts:
                drafts[drafts.index(text)] = body
        positions[index + 1] = len(messages)
    view.messages = messages
    view.speech_kinds[""] = kinds
    view.speech_ids[""] = identities
    view.debug_positions = [positions.get(int(pos), len(messages)) for pos in debug_positions]
    # Engine indices include interleaved debug echoes. Match the final ordered
    # entries before transmission so echoes can never acquire speech receipts.
    ordered_wire = {}
    entries = [(text, i) for i, text in enumerate(view.messages)] if instant else view.ordered_entries()
    for position, (text, index) in enumerate(entries):
        if index is not None and (target, index) in wire:
            ordered_wire[(target, position + echo_count)] = wire[(target, index)]
    token = WIRE.set(ordered_wire)
    try:
        yield
    finally:
        WIRE.reset(token)
        for row in ordered_wire.values():
            cleanup = getattr(row["event"], "cleanup_temporary_local_files", None)
            if callable(cleanup):
                cleanup()


async def accepted(plugin, target, text, position):
    row = (WIRE.get() or {}).get((target, position))
    if not row or row["text"] != text or row["accepted"]:
        return
    row["accepted"] = True
    event = row["event"]
    from astrbot.core.provider.entities import LLMResponse
    response = LLMResponse(role="assistant", completion_text=text)
    from ..core.engine.life_inference import released_life_lock
    async with released_life_lock(plugin.engine):
        await append_history(plugin, target, event, text)
        # Memory integrations observe only platform-accepted public content.
        await fire_hook(plugin, event, "OnLLMResponseEvent", response, accepted=True)
        await fire_hook(plugin, event, "OnAfterMessageSentEvent")


async def append_history(plugin, target, event, text):
    manager = plugin.context.conversation_manager
    from astrbot.core.utils.session_lock import session_lock_manager
    async with session_lock_manager.acquire_lock(target):
        cid = await manager.get_curr_conversation_id(target)
        req = event.get_extra("provider_request")
        expected = getattr(getattr(req, "conversation", None), "cid", None)
        # Do not append to a conversation switched/reset during inference.
        if req is not None and cid != (expected or None):
            return
        if not cid:
            cid = await manager.new_conversation(target, event.get_platform_id())
        conversation = await manager.get_conversation(target, cid) if cid else None
        if conversation is None:
            return
        history = json.loads(conversation.history or "[]")
        history.append({"role": "assistant", "content": text})
        await manager.update_conversation(target, cid, history=history)
