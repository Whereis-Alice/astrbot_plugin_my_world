"""把宿主事件里的引用补成可与聊天记录对照的上下文。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

from ..core.message_parts import _quoted_parts
from ..core.prompt import clip_line

if TYPE_CHECKING:
    from ..core.engine import MyWorldEngine


async def quoted_message_note(engine: MyWorldEngine, event: AstrMessageEvent) -> str:
    """引用正文单独标明来源，不能靠留档或相似开头推断最终提示词可见性。"""

    who, text = _quoted_parts(event)
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    someone = who or "有人"
    context = engine.world.context
    limit = max(0, int(getattr(context, "quote_chars", 1000) or 0))
    if limit <= 0:
        return f"他引用了{someone}的消息；按配置不补充引用正文，这是引用，不是他这次打的字"
    shown = body if len(body) <= limit else clip_line(body, limit)
    return (
        f"他引用了{someone}的消息，原文：「{shown}」——这是引用，不是他这次打的字"
    )
