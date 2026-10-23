"""消息与主动表达的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip, _join, _reasoning_text, node_name


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind == "user_message":
        who = _clip(detail.get("user") or "有人", 24)
        flag = "，@了她" if detail.get("wake") else ""
        return f"{who} 说话（{detail.get('len', 0)} 字{flag}）"

    if kind == "reply":
        said = _join(detail.get("messages"), sep="\n　　")
        who = detail.get("user") or ""
        head = f"回复 {who}：{said}" if who else f"回复：{said}"
        reason = _reasoning_text(detail.get("reasoning"))
        text = f"{head}　〔她的判断：{reason}〕" if reason else head
        actions = [str(item) for item in (detail.get("actions") or []) if item]
        if actions:
            text += f"　〔动作：{'、'.join(actions)}〕"
        mode = str(detail.get("plan_mode") or "")
        if mode == "interrupt":
            text += "　⏭️ 这一步插队：手上的事让开了"
        elif mode == "replace":
            text += "　⏭️ 顶上：没做的那几步不做了"
        # 她判出来"这句其实是在跟别人说话"时标一下：群里那种没 @ 的「你倒是说啊」
        # 最容易被她当成在问自己，日志里能直接核对
        if str(detail.get("addressing") or "") == "others":
            text += "　🙉 她判断这句是在跟别人说话"
        for node_id in detail.get("auto_travel") or []:
            text += f"\n　　↪ 她想去别处做这件事，已自动前往「{node_name(str(node_id), world)}」"
        images = int(detail.get("images") or 0)
        if images:
            text += f"　（还贴了 {images} 张图）"
        for warning in detail.get("warnings") or []:
            text += f"\n　　⚠ {warning}"
        return text

    if kind in {"speech_decision", "speech_result", "speech_decision_failed"}:
        trigger = {"message": "新消息", "browse": "翻消息完成", "plan": "计划接话",
                   "completion": "动作完成", "expression": "生活表达"}.get(detail.get("trigger"), "主动表达")
        if kind == "speech_decision_failed":
            return trigger + "：未形成有效发言决定（" + str(detail.get("reason") or "模型结果不可用") + "）"
        intent = {"join_chat": "接当前话题", "continue_chat": "继续之前的对话", "life_share": "分享生活",
                  "outreach": "主动找人", "listen": "继续旁听"}.get(detail.get("intent"), "表达")
        status = {"prepared": "待发送", "sent": "已发送", "partial": "部分发送", "not_sent": "未发出",
                  "listening": "仅有想法"}.get(detail.get("status"), "")
        return f"{trigger} → {intent} · {status}"

    if kind == "bot_message":
        text = f"主动发言：{_join(detail.get('messages'), sep='\n　　')}"
        images = int(detail.get("images") or 0)
        if images:
            text += f"　（还贴了 {images} 张图）"
        return text

    if kind == "send_failed":
        said = _join(detail.get("messages"), sep=" / ", limit=2)
        note = _clip(detail.get("note"), 60)
        return f"这条没能发出去（{note}）：{said}"

    if kind == "engagement":
        return f"进入安静期：{_clip(detail.get('reason'), 80)}"

    if kind == "takeover_failed":
        # 接管没生效：这一条她没说话（不交回主人格），原因写在这儿供排查
        reason = _clip(detail.get("reason"), 60) or "没有对外输出"
        head = f"这条没接上，她没说话：{reason}"
        if compact:
            return head
        warnings = [str(item) for item in (detail.get("warnings") or []) if str(item)]
        if warnings:
            head += f"　〔{_clip('；'.join(warnings), 100)}〕"
        return head

    if kind == "bot_spoke":
        return "她说了话，等待回应"

    if kind == "incoming":
        who = _clip(detail.get("user") or detail.get("user_id"), 20) or "有人"
        body = _clip(detail.get("text"), 60)
        marks = []
        if detail.get("mentioned"):
            marks.append("喊了她")
        elif detail.get("wake"):
            marks.append("交给了她")
        if detail.get("private"):
            marks.append("私聊")
        if int(detail.get("images") or 0):
            marks.append(f"{int(detail.get('images'))} 张图")
        tail = f"（{'、'.join(marks)}）" if marks else ""
        return f"收到消息：{who}：{body}{tail}"

    raise KeyError(kind)
