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
        return bool(event.is_wake_up() or _is_at_bot(event) or event.is_private_chat())

    def _queue_ambient_participation(self, event: AstrMessageEvent) -> None:
        """A stopped router event also owns its decision to remain silent."""
        if not self.enabled or self.retired or self.in_self_initiated_call():
            return
        if _is_command_event(event) or self._should_expect_reply(event):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        if not str(event.get_message_str() or "").strip() and not _image_components(event):
            return
        self.engine.queue_participation(event.unified_msg_origin)
