"""Recognise inherited stock prose without erasing authored relationship notes."""
from __future__ import annotations

from typing import Any


FAMILIARITY_DESCRIPTIONS = {
    "敌意": "目前彼此的相处距离很远。",
    "冷淡": "目前彼此的相处距离较远。",
    "客气": "你对这个人还不熟悉，了解有限。",
    "熟人": "你已经认得这个人，有一些了解。",
    "朋友": "你对这个人已有一定熟悉感。",
    "亲近": "你对这个人较熟悉，相处距离较近。",
    "特别的人": "你对这个人很熟悉，相处距离很近。",
    "戒备": "目前彼此的相处距离很远。",
    "疏远": "目前彼此的相处距离较远。",
    "礼貌": "你对这个人还不熟悉，了解有限。",
    "初识": "你刚开始认识这个人，了解还不多。",
    "熟悉": "你已经认识这个人，逐渐熟悉。",
    "投缘": "你对这个人已有熟悉感，相处较自然。",
    "信任": "你对这个人较熟悉，了解较多。",
    "亲密": "你对这个人很熟悉，相处距离很近。",
    "知己": "你对这个人很熟悉，有较深的了解。",
    "相依": "你对这个人非常熟悉，相处距离很近。",
}
"""Optional distance context; never prescribes a tone, action or relationship identity."""


INHERITED_LEVEL_PROMPTS = {
    "敌意": "你对他没什么好脸色：话短、带刺，不主动搭话，也不做任何亲昵动作。",
    "冷淡": "你对他淡淡的：客气但不热络，不主动找他，也不撒娇。",
    "客气": "你们还不熟：礼貌、有分寸，不主动提私事，不撒娇，也不动手动脚。",
    "熟人": "算是熟人了：可以闲聊、开玩笑，但别越界——不撒娇、不贴贴。",
    "朋友": "你们是朋友：说话随意、会互相吐槽、可以拍一下摸一下头，但到抱抱亲亲就过了。",
    "亲近": "你跟他很亲近：会撒娇、会抱抱、会分享日常自拍，夜里他找你也会回。",
    "特别的人": "他是你最特别的人：亲昵动作都可以，会吃醋、会闹脾气、也会主动找他。",
    "戒备": "你对这个人保持戒备，可以拒绝、回避并说清边界，不必强行温柔。",
    "疏远": "你暂时不想靠近，话少而有分寸；给彼此空间，别把冷淡演成永久敌对。",
    "礼貌": "礼貌回应，先了解对方；不预设共同过去，不强行亲昵。",
    "初识": "已经认得这个人，可以接上小话题，慢慢了解喜好，仍保留私事和身体距离。",
    "熟悉": "相处逐渐自然，可以接玩笑、记得近况；不因熟悉就默认对方接受亲昵。",
    "投缘": "愿意分享兴趣、一起做事；安慰或靠近要结合人设、场合和对方意愿。",
    "信任": "可以谈烦恼、请对方帮忙，也有自己的意见；信任不等于服从或恋爱。",
    "亲密": "相处亲近，愿意表达依赖和关心；亲昵仍遵循身份、人设、场合及双方意愿。",
    "知己": "彼此理解，可以分享脆弱和重要打算；保留各自生活，不自动宣称恋爱或占有。",
    "相依": "这个人对你意义深重，你愿意长期关心和共同规划；如何亲近由确认的关系、人设和双方意愿决定。",
}


INHERITED_LEVEL_DENY = {
    "敌意": "hug kiss pat selfie leg_photo pour_tea listen cheer share",
    "冷淡": "hug kiss pat selfie leg_photo pour_tea",
    **dict.fromkeys(("客气", "熟人", "戒备", "疏远", "礼貌", "初识"), "hug kiss pat selfie leg_photo"),
    "朋友": "hug kiss leg_photo", "熟悉": "hug kiss pat leg_photo",
    **dict.fromkeys(("投缘", "信任"), "kiss leg_photo"),
    **dict.fromkeys(("亲近", "亲密", "知己"), "leg_photo"),
}


def clear_inherited_level_prompt(data: Any) -> Any:
    """Retire tier controls; clear exact inherited defaults, preserving authored edits."""
    if not isinstance(data, dict):
        return data
    data = {key: value for key, value in data.items() if key not in ("address", "proactive_per_day")}
    name, text = data.get("name"), data.get("prompt")
    if not data.get("prompt_customized") and isinstance(name, str) and isinstance(text, str) and text.strip() == INHERITED_LEVEL_PROMPTS.get(name):
        data["prompt"] = ""
    inherited = INHERITED_LEVEL_DENY.get(name, "").split() if isinstance(name, str) else []
    if not data.get("deny_customized") and inherited and set(data.get("deny") or []) == set(inherited):
        data["deny"] = []
    return data
