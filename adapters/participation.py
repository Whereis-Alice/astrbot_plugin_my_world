"""Late AstrBot hook: routers and direct Agent replies own the event first."""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..core.message_parts import _image_components
from ..core.messages import _is_at_bot, _is_command
from ..core.request_context import _is_command_event


class ParticipationHooksMixin:
    def _should_expect_reply(self, event: AstrMessageEvent) -> bool:
        """Only direct conversations join the native reply merge queue."""
        text = str(event.get_message_str() or "")
        if _is_command(text) or _is_command_event(event):
            return False
        if event.get_extra("helper_tools_wake_block_llm_request", False):
            return False
        # is_wake_up also means an ALL/GROUP listener matched. AstrBot's
        # native dispatch flag covers real @, wake prefixes and bot quotes.
        if _is_at_bot(event) or event.is_private_chat():
            return True
        if getattr(event, "is_at_or_wake_command", False) or getattr(event, "call_llm", False):
            return True
        if event.get_extra("provider_request", None) is not None:
            return True
        # Explicit router handoff owns the event even on older wrappers.
        if event.get_extra("intent_router_no_at", False):
            return True
        # Old third-party wrappers may not expose the native flag at all.
        return not hasattr(event, "is_at_or_wake_command") and bool(event.is_wake_up())

    def _queue_ambient_participation(self, event: AstrMessageEvent) -> None:
        """A stopped router event also owns its decision to remain silent."""
        if not self.enabled or self.retired or self.in_self_initiated_call():
            return
        if event.is_stopped() or event.get_extra("helper_tools_wake_block_llm_request", False):
            return
        if _is_command_event(event) or self._should_expect_reply(event):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        if not str(event.get_message_str() or "").strip() and not _image_components(event):
            return
        self.engine.queue_participation(event.unified_msg_origin)
