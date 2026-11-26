"""Verify the event behind a model's assessment of an interpersonal turn."""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import unquoted_segments


AFFINITY_CHANGE = {"kindness": .05, "support": .15, "repair": .20, "hurt": -.20, "breach": -.60}

INTERACTION_GUIDANCE = (
    'interaction 默认 null。只有本轮对她的实际行为值得改变长期感受时，填写 '
    '{"kind":"行为类别","source_id":"直接发言证据里的 id","quote":"本轮原话","reason":"这次具体做了什么"}。\n'
    'kindness 是有具体细节的善意，support 是实际帮忙或认真支持，repair 是对具体伤害负责并确实有修补；'
    'hurt 是明确针对她的人身贬低或直接伤害。breach 只用于本轮有确切依据的严重侵犯、威胁或明显背信，'
    '如明确承认故意公开承诺保密的内容。语气冲或她一时难过不足以判为 breach。\n'
    '区分她的性格、熟人打趣与真实攻击：礼貌寒暄、不同意见、合理拒绝和表达边界、引用别人的话、'
    '复述旧事、反话或拿不准谁是对象时，保持 null。只引用当事人这轮自己的原话，'
    '不把她的回复、已有好感或背景故事当作行为证据，也不根据单个词给人定性。'
)


@dataclass(frozen=True, slots=True)
class InteractionJudgment:
    kind: str
    source_id: str
    quote: str
    reason: str


@dataclass(frozen=True, slots=True)
class TurnInteraction:
    affinity_delta: float = 0.0
    grudge_allowed: bool = False
    harmful: bool = False

    def tone(self, suggested: str) -> str:
        # Word matching alone cannot turn a disagreement or a quote into an attack.
        if self.harmful:
            return "attack"
        return "normal" if suggested in {"attack", "negative"} else suggested


def parse_interaction(item: object) -> InteractionJudgment | None:
    if not isinstance(item, dict):
        return None
    values = [item.get(key) for key in ("kind", "source_id", "quote", "reason")]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    kind, source_id, quote, reason = (" ".join(value.split()) for value in values)
    if kind not in AFFINITY_CHANGE or not source_id.startswith("src_") or not 2 <= len(quote) <= 300:
        return None
    return InteractionJudgment(kind, source_id, quote, reason[:200])


def interaction_source(judgment: InteractionJudgment | None, sources: list[dict],
                       session: str, user: str, addressing: str) -> dict | None:
    if judgment is None or addressing == "others":
        return None
    for source in sources:
        known_addressing = source.get("addressing")
        if (source.get("id") != judgment.source_id or source.get("session") != session
                or str(source.get("user_id") or "") != user or source.get("kind") != "direct_speech"
                or known_addressing == "others" or addressing != "me" and known_addressing != "me"):
            continue
        if any(judgment.quote in segment for segment in unquoted_segments(str(source.get("text") or ""))):
            return source
    return None
