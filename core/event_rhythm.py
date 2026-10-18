"""事件的类别抽样与时段判断，独立于模型生成。"""

from __future__ import annotations

from typing import Any

TONES = {
    "daily": ("日常", "平静、普通的一刻：顺手完成小事、观察环境、熟悉的生活。不要硬加事故或挫折。"),
    "good": ("好事", "一次小收获、顺利的尝试、善意或惊喜。好结果可以直接发生，不必先遇到麻烦。"),
    "bad": ("坏事", "有限的损失或不顺，保持可处理的尺度，不升级成连续灾难。"),
    "annoying": ("糟心事", "琐碎的打扰、尴尬或小挫折，允许放下，不扩大为伤害。"),
    "strange": ("怪事", "意料之外但能解释的巧合、有趣发现或新鲜体验，遵守场景和事件禁区。"),
}


def in_event_hours(hour: int, start: int, end: int) -> bool:
    """左闭右开；相同起止表示全天，支持跨午夜。"""
    if start == end:
        return True
    return start <= hour < end if start < end else hour >= start or hour < end


def pick_tone(weights: dict[str, float], rng: Any) -> str:
    pool = [(key, max(0.0, float(weights.get(key, 0)))) for key in TONES]
    total = sum(weight for _, weight in pool)
    if total <= 0:
        return "daily"
    point = rng.random() * total
    for key, weight in pool:
        if weight <= 0:
            continue
        point -= weight
        if point < 0:
            return key
    return next(key for key, weight in reversed(pool) if weight > 0)
