"""处理宿主请求里的提示词拼接：摘掉宿主人设、分离临时上下文。
"""

from __future__ import annotations

import json
import re
from typing import Any

try:  # AstrBot 4.24.2+：附加内容块走这条正规通道
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 老版本没有这个类
    TextPart = None  # type: ignore[assignment]

from .messages import _is_command


_ASTRBOT_PERSONA_HEAD = "# Persona Instructions"


def _drop_persona_instructions(text: str, persona_text: str = "") -> str:
    """把 AstrBot 拼进来的「# Persona Instructions」那一段摘掉。

    AstrBot 在 `build_agent` 里就把「人格设定」追加到 ``req.system_prompt``
    （`astrbot/core/astr_main_agent.py`），而插件自己已经按 ``persona.mode``
    把人设放进了她的提示词——再带一遍等于同一份人设出现两次，
    「其他插件提供的上下文」那一节还会把 AstrBot 那份原样念给她听。

    其余插件写进 system_prompt 的内容（上下文理解、记忆…）原样保留。

    ``persona_text`` 是宿主这一轮真正写进去的那份人设原文（拿得到就给，见
    ``MyWorldPlugin._host_persona_prompt``）。有它就按原文精确摘掉——这一步很关键：
    人设块后面经常紧跟着别的插件写的内容（世界树的 ``<worldtree_persona>``、
    有的插件直接拼一段正文），按"摘到下一个一级标题"去猜会把那些内容一起吞掉。
    """

    body = str(text or "")
    index = body.find(_ASTRBOT_PERSONA_HEAD)
    if index < 0:
        return body.strip()
    needle = str(persona_text or "").strip()
    if needle:
        found = body.find(needle, index + len(_ASTRBOT_PERSONA_HEAD))
        if found >= 0:
            end = found + len(needle)
            # 只吃掉人设块自己的换行，后面紧跟的内容一个字都不动
            while end < len(body) and body[end] == "\n":
                end += 1
            head = body[:index].rstrip()
            rest = body[end:].lstrip()
            return f"{head}\n\n{rest}".strip() if rest else head
    head = body[:index].rstrip()
    tail = body[index + len(_ASTRBOT_PERSONA_HEAD):]
    # 退而求其次：人设那一段一直写到下一个一级标题（`# xxx`）为止
    match = re.search(r"(?m)^#\s", tail)
    if not match:
        # 找不到下一个一级标题时**不猜**：以前这里会一路删到结尾，
        # 结果是把紧跟人设后面的插件内容（没有小标题的那种）一起吞了。
        # 宁可人设多出现一次，也不能丢别人的上下文。
        return body.strip()
    rest = tail[match.start():]
    return (head + "\n" + rest.lstrip("\n")).strip()


def _merged_request_context(req: Any, base: str = "", extra: str = "") -> str:
    """别的插件写进这次请求的上下文：``base``（system_prompt）＋临时正文。

    AstrBot 4.24.2+ 用这个字段承载「只对当前这一轮有效、不写进对话历史」的内容，
    世界树（worldtree_lore）的设定、helper_tools 的环境感知都往这儿写。
    本插件接管时是自己拼提示词、自己发请求的，AstrBot 那条「拼进正文」的路不会跑，
    所以得把这些片段取出来一起带上——不带的话，装了它们的用户会发现注入突然失效。

    ``extra`` 是调用方另外摘出来的临时信息（例如老版本 AstrBot 上被拼进
    ``req.prompt`` 的那一段，见 :func:`_split_prompt_injection`）。
    老版本 AstrBot 没有 ``extra_user_content_parts`` 这个字段，取不到就当没有。
    """

    texts: list[str] = [str(base or "").strip(), str(extra or "").strip()]
    parts = getattr(req, "extra_user_content_parts", None)
    if parts:
        try:
            for part in list(parts):
                text = str(getattr(part, "text", "") or "").strip()
                if text:
                    texts.append(text)
        except Exception:
            pass
    return "\n\n".join(item for item in texts if item)


def _append_extra_text(req: Any, text: str) -> None:
    """把「易变的一层」挂进宿主的这次请求。

    优先走 ``extra_user_content_parts``（AstrBot 4.24.2+ 的正规通道，跟着用户消息
    一起送进模型、不写进对话历史）；请求对象上没有这个字段、或者拿不到
    ``TextPart`` 时，退回到往 ``req.prompt`` 尾巴上追加，并带上标记，
    别的插件和日志都认得出这一段不是用户说的话。
    """

    body = str(text or "").strip()
    if not body:
        return
    marked = f"[我的世界·此刻]\n{body}"
    parts = getattr(req, "extra_user_content_parts", None)
    if isinstance(parts, list) and TextPart is not None:
        try:
            part = TextPart(text=marked)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                mark()
            parts.append(part)
            return
        except Exception:
            pass
    original = str(getattr(req, "prompt", "") or "")
    req.prompt = f"{original}\n\n{marked}".strip()


def _safe_brief(value: Any, limit: int = 160) -> str:
    """把工具参数压成一行短文本：日志和复盘用，不能因为参数太大把内存撑炸。"""

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


TEMP_CONTEXT_MARKERS = ("[环境感知]",)
"""别人往 ``req.prompt`` 尾巴上直接拼的「临时信息」标记。

AstrBot 4.24.2+ 有 ``extra_user_content_parts`` 这条正规通道（helper_tools 的
环境感知、世界树的设定都走它）；更老的版本上插件会退化成"往用户那句话后面拼一段"。
那段内容不是用户说的话，直接当成用户正文会被她理解成"群友说今天是法定节假日"，
所以按标记摘出来，放进「其他插件提供的上下文」。

想要扩展这个清单的话，加标记即可——只在"空行 + 标记"这种边界上才认，
免得用户在正文里随手打出这几个字也被拆开。
"""


def _split_prompt_injection(prompt: str) -> tuple[str, str]:
    """把 ``req.prompt`` 里被追加的临时信息摘出来：返回（用户说的话, 临时信息）。"""

    text = str(prompt or "")
    if not text:
        return "", ""
    cut = len(text)
    for marker in TEMP_CONTEXT_MARKERS:
        for needle in (f"\n\n{marker}", f"\n{marker}"):
            index = text.find(needle)
            if index <= 0:
                continue
            cut = min(cut, index + len(needle) - len(marker))
    if cut >= len(text):
        return text.strip(), ""
    return text[:cut].strip(), text[cut:].strip()


def _selected_provider_extra(event: Any) -> str:
    """别的插件（例如世界树）在这一轮里临时指定的模型 id。

    它在 ``on_waiting_llm_request`` 里把 ``selected_provider`` 挂到事件上，
    宿主随后就用它去调模型。本插件接管时会自己调模型，所以同样得认这一眼，
    否则「这一条用另一个模型回答」这类配置在接管场景下会静默失效。
    """

    try:
        raw = event.get_extra("selected_provider", "")
    except Exception:
        return ""
    return str(raw or "").strip()


def _has_voice_component(event: Any) -> bool:
    """这条消息里有没有语音段（Record/Voice）。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ in ("Record", "Voice"):
            return True
    return False


def _is_command_event(event: Any) -> bool:
    """这条消息是不是一条插件指令（`/mw …`、`/help` …）。

    AstrBot 在"唤醒检查"那一步就把唤醒前缀从 ``message_str`` 里摘掉了
    （`/mw event h` 到了插件手里只剩 `mw event h`），所以光看开头是不是 `/` 认不出来。
    框架会把**命中的指令参数**塞进 ``handlers_parsed_params``，用它来认最准。
    """

    try:
        if event.get_extra("handlers_parsed_params"):
            return True
    except Exception:
        pass
    try:
        text = str(event.get_message_str() or "")
    except Exception:
        text = ""
    return _is_command(text)
