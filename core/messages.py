"""读取进入插件的消息：@、引用、身份、指令判断。
"""

from __future__ import annotations

import time
from typing import Any

from .constants import MW_EVENT_ID_EXTRA


def _is_at_bot(event: Any) -> bool:
    """这条消息里是不是真的 @ 了她。

    不能只看 ``event.is_wake_up()``：意图路由那类插件会在放行时把 ``is_wake`` 置 True，
    那是"要交给大模型"，不等于"有人在跟她说话"。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    if not self_id:
        return False
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "At":
            continue
        qq = str(getattr(component, "qq", "") or "")
        if qq and qq == self_id:
            return True
    return False


def _is_soft_wake(event: Any) -> bool:
    """意图路由放行时补的 @ 是假的：原话里没人 @ 她，只是"顺着话题对她说"。"""

    try:
        return bool(event.get_extra("intent_router_no_at", False))
    except Exception:
        return False


def _router_float(value: Any) -> float:
    """路由给的分数（拿不到就当 0）。"""

    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _router_view(event: Any) -> dict:
    """意图路由放行时交过来的事前判定（拿不到 / 形状不对就是空 dict）。

    路由在重投那一刻把**整批**判定挂在事件上（见它的 README「交给她的事前判定」）：
    ``batch`` 按到达顺序列出这一批每一条，含被合并成一次回复、正文根本没进请求的
    那几条，``released`` 标出真正放行的。价值在于「这句是不是冲她说的」这件事
    **回复之前**就有答案了——不用等她回完再让主模型自己猜（那是 ``addressing``，
    见 :meth:`Engine._mark_reply_addressing`）。

    这里只做形状校验和取值：判定本身是路由给的，本插件不重新解释。
    """

    try:
        raw = event.get_extra("intent_router_decision", None)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    rows = raw.get("batch")
    if not isinstance(rows, list):
        return {}
    batch: list[dict] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        batch.append(
            {
                "sender": str(entry.get("sender") or ""),
                "sender_id": str(entry.get("sender_id") or ""),
                "message_id": str(entry.get("message_id") or ""),
                "text": str(entry.get("text") or ""),
                "released": bool(entry.get("released")),
                "worth": bool(entry.get("worth")),
                "directed": bool(entry.get("directed")),
                "to": str(entry.get("to") or ""),
                "reply_score": _router_float(entry.get("reply_score")),
                "reason": str(entry.get("reason") or ""),
            }
        )
    if not batch:
        return {}
    return {"policy": str(raw.get("policy") or ""), "batch": batch}


def _group_name(event: Any) -> str:
    """平台报的群名（拿不到就返回空串，提示词里就只显示群号 / 备注）。"""

    getter = getattr(event, "get_group_name", None)
    if callable(getter):
        try:
            value = " ".join(str(getter() or "").split())
        except Exception:
            value = ""
        if value:
            return value[:40]
    group = getattr(getattr(event, "message_obj", None), "group", None)
    for attr in ("group_name", "name"):
        value = " ".join(str(getattr(group, attr, "") or "").split())
        if value:
            return value[:40]
    return ""


def _mention_note(event: Any) -> str:
    """这条消息 @ 了谁——只说结论，不讲平台怎么处理正文。

    她不知道自己的 QQ 号，所以别人 @ 她的时候，光看文本里的 ``@昵称`` 她分不清
    那是不是在叫她。这里直接用「你」标出"有没有 @ 她"，再点出"@ 的别人不是在
    叫她"，别把别人的话揽到自己身上。

    第三个坑：**意图路由补的那个 @ 是假的**（它只是为了让消息重新走通管道）。
    照着它写"这条消息 @ 了你"，她会以为对方真的点了她——而用户看到的是一句
    平白多出来的"@ 了你"。这种消息只留一句"是顺着话题说到你的"。

    以前这里还会解释「平台把开头的 @ 自己省掉了」「你说的这段正文里可能看不到
    那一下」——那段是写给开发者看的，她只要知道结论就够了，去掉。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    soft = _is_soft_wake(event)
    message_obj = getattr(event, "message_obj", None)
    others: list[str] = []
    mine = False
    everyone = False
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ not in ("At", "AtAll"):
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        if soft and qq == self_id:
            # 路由补的假 @：不算"点了她"，也别写进记录
            continue
        name = str(getattr(component, "name", "") or "").strip()
        if qq == "all":
            everyone = True
            continue
        label = _at_label(name, qq)
        if not label:
            continue
        if qq and self_id and qq == self_id:
            mine = True
            continue
        # @ 的是别人：这段不能让她以为是在叫她
        others.append(label)
    if soft and not others and not everyone:
        return "这段没 @ 你，是顺着上面的话题说到你的"
    if not mine and not others and not everyone:
        return ""
    named = "、".join(dict.fromkeys(others))
    all_note = "；还 @ 了所有人" if everyone else ""
    if mine:
        if named:
            return f"这条消息 @ 了你；同一条里 @ 的 {named} 不是你{all_note}"
        return "这条消息 @ 了你，也 @ 了所有人" if everyone else "这条消息 @ 了你"
    if everyone:
        return (
            f"这条消息 @ 了所有人，还有 {named}（都不是在 @ 你）"
            if named
            else "这条消息 @ 了所有人（包括你）"
        )
    return f"这条消息 @ 的 {named} 不是你（别揽到自己身上）"


def _at_label(name: str, qq: str) -> str:
    """@ 目标的标签：名字和号码尽量都给，但别写成 ``2829449702(2829449702)``。"""

    clean_name = " ".join(str(name or "").split())
    clean_qq = str(qq or "").strip()
    if not clean_name:
        return clean_qq
    if not clean_qq or clean_name == clean_qq:
        return clean_name
    return f"{clean_name}({clean_qq})"


def _message_id(event: Any) -> str:
    """平台给的消息 id（拿不到就是空串）。

    这个 id 只用来**认"是不是同一个事件"**，不参与"这个平台支不支持某功能"的判定，
    所以不像 ``_quote_target`` 那样先按平台名挡一道。
    """

    message_obj = getattr(event, "message_obj", None)
    return str(getattr(message_obj, "message_id", "") or "").strip()


def _stable_event_id(event: Any) -> str:
    """没有平台 message_id 时，为这条消息生成一个**稳定**的内部事件 id。

    存在 event extra 里：旁观监听和 LLM 请求两个钩子拿到的是同一个 event 对象，
    所以两边拿到的是同一个 id——正文被注解改过也不会变成"另一条消息"。
    """

    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            existing = str(getter(MW_EVENT_ID_EXTRA, "") or "")
        except Exception:
            existing = ""
        if existing:
            return existing
    created = f"{int(time.time_ns())}-{id(event)}"
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        try:
            setter(MW_EVENT_ID_EXTRA, created)
        except Exception:
            return created
    return created


def _incoming_identity(event: Any, raw_text: str) -> dict:
    """一条消息的事件身份 + 结构化标注（登记待回队列、写留档都用它）。

    有平台 ``message_id`` 时按 ``origin + message_id`` 认同一个事件；没有就用
    :func:`_stable_event_id` 生成的内部 id。``raw_text`` 是注解**之前**的原文——
    事件身份认的是这条消息本身，注解（图片说明 / @ / 转发摘要）改不了它。
    """

    message_id = _message_id(event)
    event_id = "" if message_id else _stable_event_id(event)
    targets = _at_targets(event)
    reply_to = _reply_target(event)
    self_id = ""
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            self_id = str(getter() or "")
        except Exception:
            self_id = ""
    sender_id = ""
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            sender_id = str(getter() or "")
        except Exception:
            sender_id = ""
    sender_name = ""
    getter = getattr(event, "get_sender_name", None)
    if callable(getter):
        try:
            sender_name = str(getter() or "")
        except Exception:
            sender_name = ""
    return {
        "message_id": message_id,
        "event_id": event_id,
        "user_id": sender_id,
        "user_name": sender_name,
        "raw_text": str(raw_text or ""),
        "at_targets": targets,
        "reply_to": reply_to,
        "addressing": _addressing_kind(targets, reply_to, self_id),
    }


def _at_targets(event: Any) -> list[dict]:
    """这条消息 @ 了谁（含她自己），结构化一份。

    意图路由为了让消息重新走通管道，会往消息链里**补一个假的 @ 她自己**
    （见 `_is_soft_wake`）。那个 @ 不是用户打的，必须剔掉——否则她的记录里
    会出现"这条消息 @ 了你"，而她明明只是顺着话题被叫起来。
    """

    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    soft = _is_soft_wake(event)
    message_obj = getattr(event, "message_obj", None)
    targets: list[dict] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ not in ("At", "AtAll"):
            continue
        qq = str(getattr(component, "qq", "") or "").strip()
        if soft and qq == self_id:
            continue
        name = " ".join(str(getattr(component, "name", "") or "").split())
        if qq == "all":
            targets.append({"id": "all", "name": "所有人", "self": False})
            continue
        if not qq and not name:
            continue
        targets.append(
            {
                "id": qq,
                "name": name or qq,
                "self": bool(qq and self_id and qq == self_id),
            }
        )
    return targets


def _reply_target(event: Any) -> dict:
    """这条消息引用 / 回复的是谁（拿不到就是空的）。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        uid = str(
            getattr(component, "sender_id", "") or getattr(component, "qq", "") or ""
        ).strip()
        name = " ".join(
            str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).split()
        )
        if uid or name:
            return {"id": uid, "name": name or uid}
    return {}


def _addressing_kind(
    targets: list[dict], reply_to: dict, self_id: str
) -> str:
    """这句是冲谁说的：``me``（点了她）/ ``others``（在叫别人）/ ``""``（看不出来）。

    只用来标注，不参与"要不要回复"的判定——那个还是走 ``ctx.is_mentioned`` 那一套。
    """

    mine = any(item.get("self") for item in targets or [])
    if str((reply_to or {}).get("id") or "") and str((reply_to or {}).get("id")) == str(
        self_id or ""
    ):
        mine = True
    if mine:
        return "me"
    if targets or reply_to:
        return "others"
    return ""


def _is_command(text: str) -> bool:
    """看起来是一条指令（/xxx、！xxx 这类）。指令永远不该被她睡觉挡住。"""

    stripped = (text or "").lstrip()
    return bool(stripped) and stripped[0] in "/!！.。"
