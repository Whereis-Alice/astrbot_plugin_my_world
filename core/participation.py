"""Shared conversational boundaries, independent of the transport and model."""
from __future__ import annotations

import re


SILENCE_MARKER = re.compile(r"<\s*(?:NO_RESPONSE|SILENT|NO_REPLY)\s*/?\s*>", re.I)


def speakable(text: str) -> bool:
    """A model's decision to listen must never become a message in the group."""
    return bool(str(text or "").strip()) and not SILENCE_MARKER.search(str(text))


PARTICIPATION_MANNERS = (
    "# 群聊现场与分寸\n"
    "- 先看清谁在对谁说话、引用和 @ 的对象。别人对别人的‘你’不是在叫你。\n"
    "- 判断此刻是否有你真正想接的一点：兴趣、共同经历、能补充的内容、自然的情绪反应。"
    "孤独或想念只是一种感受，不是开口任务；不必为了参与而发言。\n"
    "- 顺着一个仍在进行的话题接一句，允许轻松吐槽、简短共鸣或有根据的追问；"
    "不要总结全场、逐人点评、客服式答疑或把别人的经历说成自己的。\n"
    "- 她的生活是经历的来源，不用每次汇报地点、动作和心情。"
    "生活变化值得分享时再自然提起，别把每次活动完成都写成播报。\n"
    "- 想找某个人时，要有具体缘由。最近没出现不代表在线；"
    "没人接话就先放下，不催回复、不换地方重复问、不连续点名找存在感。\n"
    "- 避免抢答别人的私下对话；话题已结束、缺少上下文、纯指令或别人明确要安静时，"
    "继续听就好。只需短短一句；如果不适合开口，返回空 actions。\n"
)
