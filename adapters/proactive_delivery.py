"""Run response/decorating hooks on public speech, preserving delivery identity."""
from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from .proactive_context import isolated_event, source_event
from .event_snapshot import snapshot_request
from .proactive_hooks import fire_hook as _fire_hook
from .group_mentions import has_body

if TYPE_CHECKING:
    from astrbot.core.provider.entities import LLMResponse

WIRE = ContextVar("my_world_proactive_wire", default=None)


def public_response(text: str, source: LLMResponse | None = None) -> LLMResponse:
    """Give response hooks a native, detached chain containing only public text."""
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.provider.entities import LLMResponse

    response = copy.copy(source) if source is not None else LLMResponse(role="assistant")
    # Detach before setting text: the native setter edits an existing chain.
    response.result_chain = MessageChain(chain=[Plain(text=text)])
    response.completion_text = text
    response.reasoning_content = ""
    return response


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
    messages, kinds, identities, wire = [], [], [], {}
    request = original.get_extra("provider_request")
    history_target = ({"cid": getattr(getattr(request, "conversation", None), "cid", None)}
                      if request is not None else None)
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
        public_original = original
        referenced_row = None
        if kind in {"participation", "browse_reply", "continuation", "interject"}:
            reply_id = view.reply_targets.get(target, '')
            if reply_id:
                referenced_row = next((row for row in view.chat_media_scenes.get(target, {}).get("rows", [])
                    if str(row.get("message_id") or "") == reply_id
                    and str(row.get("origin") or target) == target and not row.get("is_self")), None)
                referenced = source_event(plugin, target, [{'message_id': reply_id, 'origin': target}])
                if str(getattr(getattr(referenced, 'message_obj', None), 'message_id', '') or '') == reply_id:
                    public_original = referenced
        # Request hooks may need a video's event; response hooks address the
        # actual message being answered and keep the inference conversation.
        event = isolated_event(public_original, plugin.context, target)
        if referenced_row and str(getattr(event.message_obj, "message_id", "") or "") != reply_id:
            # The media event cache is bounded. Its eviction must not make
            # response decorators address the sender of a different message.
            sender = getattr(event.message_obj, "sender", None)
            if sender is not None:
                sender.user_id = str(referenced_row.get("user_id") or "")
                sender.nickname = str(referenced_row.get("name") or referenced_row.get("user_name") or sender.user_id)
            event.message_obj.message_id = reply_id
            event.message_str = str(referenced_row.get("text") or "")
            event.message_obj.message = [Plain(text=event.message_str)]
            event.message_obj.raw_message = None
        if request is not None:
            event.set_extra("provider_request", snapshot_request(request))
        # A life share addresses the room, not the sender of its last message.
        if ":GroupMessage:" in target and kind not in {"participation", "browse_reply", "continuation", "interject"}:
            sender = getattr(event.message_obj, "sender", None)
            if sender is not None:
                person = next(iter(sorted(view.contact_people)), "") if kind == "outreach" else ""
                sender.user_id = person or event.get_group_id()
                sender.nickname = person or "群里"
        raw = plugin.take_llm_response(target)
        response = public_response(text, raw)
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
        if not has_body(chain):
            positions[index + 1] = len(messages)
            continue
        # A voice-only replacement still speaks the text; retain it for the
        # character's memory, while the transport sends only the actual chain.
        body = body or edited or str(text)
        position = len(messages)
        messages.append(body)
        kinds.append(kind)
        identities.append(identity)
        wire[(target, position)] = {"text": body, "chain": chain, "event": event,
                                   "accepted": False, "history_target": history_target}
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
    response = public_response(text)
    from ..core.engine.life_inference import released_life_lock
    async with released_life_lock(plugin.engine):
        history_error = None
        try:
            await append_history(plugin, target, event, text, history_target=row.get("history_target"))
        except Exception as exc:
            history_error = exc
        # Memory integrations observe only platform-accepted public content.
        # Their accepted hook must still run if the independent host DB fails.
        await fire_hook(plugin, event, "OnLLMResponseEvent", response, accepted=True)
        await fire_hook(plugin, event, "OnAfterMessageSentEvent")
        if history_error is not None:
            raise history_error


async def append_history(plugin, target, event, text, *, history_target=None):
    manager = plugin.context.conversation_manager
    from astrbot.core.utils.session_lock import session_lock_manager
    async with session_lock_manager.acquire_lock(target):
        cid = await manager.get_curr_conversation_id(target)
        req = event.get_extra("provider_request")
        expected = (history_target["cid"] if history_target is not None
                    else getattr(getattr(req, "conversation", None), "cid", None))
        # Do not append to a conversation switched/reset during inference.
        if (req is not None or history_target is not None) and cid != (expected or None):
            return
        if not cid:
            cid = await manager.new_conversation(target, event.get_platform_id())
            if history_target is not None:
                # Only this delivery may follow the branch it just created.
                # Independent drafts still retain their original expectation.
                history_target["cid"] = cid
        conversation = await manager.get_conversation(target, cid) if cid else None
        if conversation is None:
            return
        history = json.loads(conversation.history or "[]")
        history.append({"role": "assistant", "content": text})
        await _save_history(manager, target, cid, history)


async def _save_history(manager, target, cid, history):
    """Retry an unchanged baseline, recognizing a committed write first.

    The caller holds the native session lock throughout readback and retry.
    Do not deduplicate by text: a separate accepted turn may say the same thing.
    """
    try:
        await manager.update_conversation(target, cid, history=history)
    except Exception as failure:
        try:
            if await manager.get_curr_conversation_id(target) != cid:
                return
            conversation = await manager.get_conversation(target, cid)
            persisted = json.loads(conversation.history or "[]") if conversation is not None else None
        except Exception:
            raise failure
        if persisted == history:
            return  # The write committed; only its response failed.
        if persisted != history[:-1]:
            raise  # Missing/edited history cannot be safely overwritten.
        # Exactly one retry of the same write, never of transport or hooks.
        await manager.update_conversation(target, cid, history=history)
