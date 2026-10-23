"""把宿主事件里的引用补成可与聊天记录对照的上下文。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

from ..core.message_parts import _quoted_parts
from ..core.prompt import clip_line

if TYPE_CHECKING:
    from ..core.engine import MyWorldEngine


async def quoted_message_note(engine: MyWorldEngine, event: AstrMessageEvent) -> str:
    """记录中有完整原文时只提示开头；缺失或被截断时补上引用正文。"""

    who, text = _quoted_parts(event)
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    someone = who or "有人"
    context = engine.world.context
    line_limit = max(40, int(getattr(context, "chat_line_chars", 500) or 500))
    found = await engine.quote_in_records(event.unified_msg_origin, body)
    # 只有记录中的原文完整可读时才省略；不能让她对着截断的消息猜引用。
    if found and len(body) <= line_limit:
        head = body[:40] + ("…" if len(body) > 40 else "")
        return (
            f"他引用的是上面聊天记录里的那条（{someone}：「{head}」）"
            "——照上面那条看，这不是他这次打的字"
        )
    limit = max(0, int(getattr(context, "quote_chars", 1000) or 0))
    if limit <= 0:
        return f"他引用了一条更早的消息（{someone}），那条已经不在聊天记录里了"
    shown = body if len(body) <= limit else clip_line(body, limit)
    where = "上面聊天记录里那条太长，只显示了开头" if found else "聊天记录里已经没有了"
    return (
        f"他引用了一条更早的消息（{someone}），{where}，"
        f"原文：「{shown}」——这是引用，不是他这次打的字"
    )
