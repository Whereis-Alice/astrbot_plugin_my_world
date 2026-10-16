"""消息组件工具：判断类型、取图片、拆合并转发、拼被引用文本。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


from .state import FORWARD_SUMMARY_MARK


_IMAGE_COMPONENT_NAMES = (
    "Image",
    "MarketFace",
    "Marketface",
    "Mface",
    "Sticker",
    "CustomFace",  # 协议端自定义表情：有的会带 url/file，能当图片读
)
"""会被当成"图"处理的组件：普通图片 + QQ 商城表情 / 表情包。

QQ 的表情包（大表情）在协议端不是 ``Image``：它常常只有一个 ``summary``（那句文案）、
``emoji_id`` 和一个拿不到的 ``key``，所以以前"这张图"就整个丢了——识图模型收到空地址，
只能回一句"未提供图片"。这里把它一起收进来，取不到图时至少把文案当文字用。
"""

_FACE_COMPONENT_NAMES = ("Face", "FaceEmoji", "MarketFace", "Marketface", "Mface")
"""系统小黄脸 / 表情包：没有图可读时，用它的名字或文案当文字描述。"""


def _component_named(component: Any, name: str) -> bool:
    return type(component).__name__ == name


def _is_image_component(component: Any) -> bool:
    return type(component).__name__ in _IMAGE_COMPONENT_NAMES


def _image_components(event: Any) -> list[Any]:
    """这条消息里的图片组件，以及**被引用消息**里的图片组件。

    引用（回复）消息的图片挂在 ``Reply.chain`` 上，只看当前消息会漏掉
    「引用了一张图再问她」这种最常见的用法。
    """

    message_obj = getattr(event, "message_obj", None)
    found: list[Any] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if _is_image_component(component):
            found.append(component)
            continue
        if _component_named(component, "Reply"):
            for inner in list(getattr(component, "chain", None) or []):
                if _is_image_component(inner):
                    found.append(inner)
    return found


def _raw_image_ref(component: Any) -> str:
    """组件里现成的地址：优先 http(s)，其次文件 URI / 本地路径。"""

    for attr in ("url", "file", "path", "url_image", "image_url", "emoji_url"):
        value = str(getattr(component, attr, "") or "").strip()
        if not value:
            continue
        if attr == "path" and "://" not in value:
            try:
                return Path(value).resolve().as_uri()
            except Exception:
                return value
        return value
    return ""


def _is_usable_image_ref(ref: str) -> bool:
    """这个地址能不能直接交给 AstrBot 的模型调用（它自己会去下载/解码）。"""

    if not ref:
        return False
    try:
        from astrbot.core.utils.image_ref_utils import is_supported_image_ref
    except Exception:
        return ref.startswith(("http://", "https://", "base64://", "file://"))
    try:
        return bool(is_supported_image_ref(ref))
    except Exception:
        return False


MAX_SENT_IMAGES = 9
"""一条消息里最多贴几张结果图（和引擎那边的上限对齐，防刷屏）。"""


def _sendable_images(images: list[str] | None) -> list[str]:
    """挑出能真正发出去的图片地址：去重、限量、丢掉取不到的。"""

    picked: list[str] = []
    for item in list(images or []):
        ref = str(item or "").strip()
        if not ref or ref in picked:
            continue
        if not _is_usable_image_ref(ref):
            continue
        picked.append(ref)
        if len(picked) >= MAX_SENT_IMAGES:
            break
    return picked
async def _resolve_image_ref(component: Any) -> str:
    """把一个图片组件换成「模型能吃到」的地址。

    NapCat 这类协议端给的 ``file`` 经常只是一个文件名，直接丢给模型是取不到的；
    这时交给 AstrBot 自己的 ``convert_to_base64()``（http / file / base64 都能处理），
    换成 ``base64://`` 再送出去。
    """

    ref = _raw_image_ref(component)
    if _is_usable_image_ref(ref):
        return ref
    converter = getattr(component, "convert_to_base64", None)
    if callable(converter):
        try:
            data = await converter()
        except Exception:
            data = ""
        if data:
            return f"base64://{data}"
    return ref


async def _image_sources(event: Any) -> list[str]:
    """挑出这条消息里的图片，返回可以喂给多模态模型的地址。"""

    sources: list[str] = []
    for component in _image_components(event):
        ref = await _resolve_image_ref(component)
        # 只交"真的能取到"的地址：拿不到时留给 _face_note / 注解去说清，
        # 不然识图模型会收到一个取不到的名字，回一句"未提供图片"（等于白花一次调用）
        if ref and _is_usable_image_ref(ref) and ref not in sources:
            sources.append(ref)
    return sources


def _sticker_note(event: Any) -> str:
    """QQ 表情 / 表情包：没有图可读时，用它的名字或文案写成一句人话。

    ``Face`` 是系统小黄脸（只有 id / 名字），``MarketFace`` 是商城表情包
    （通常带一句 ``summary``，例如「笑死」）。这些以前直接丢失，她只看到空气。
    """

    message_obj = getattr(event, "message_obj", None)
    parts: list[str] = []
    for component in list(getattr(message_obj, "message", []) or []):
        if not _component_named_any(component, _FACE_COMPONENT_NAMES):
            continue
        summary = " ".join(
            str(
                getattr(component, "summary", "")
                or getattr(component, "name", "")
                or getattr(component, "text", "")
                or ""
            ).split()
        )
        face_id = str(
            getattr(component, "face_id", "")
            or getattr(component, "id", "")
            or getattr(component, "emoji_id", "")
            or ""
        ).strip()
        kind = "表情包" if _component_named(component, "MarketFace") else "表情"
        label = summary or (f"{kind} {face_id}" if face_id else kind)
        parts.append(f"对方发了个{kind}：{label}" if summary else f"对方发了个{kind}（{label}）")
    if not parts:
        return ""
    return "；".join(dict.fromkeys(parts))


def _component_named_any(component: Any, names: tuple[str, ...]) -> bool:
    return type(component).__name__ in names


def _provider_supports_images(provider: Any) -> bool | None:
    """这个 Provider 勾选的模态里有没有「图像」。

    AstrBot 把勾选结果记在 ``provider_config['modalities']``：空列表是"没配"
    （按老版本行为当作支持），没有这一项就读不到——那就交给配置里的开关决定。
    """

    config = getattr(provider, "provider_config", None)
    if not isinstance(config, dict):
        return None
    modalities = config.get("modalities", None)
    if modalities == []:
        return True
    if isinstance(modalities, list):
        return "image" in modalities
    return None


def _image_components_debug(event: Any) -> str:
    """取不到图片地址时留下的线索：协议端到底给了什么字段。"""

    parts: list[str] = []
    for component in _image_components(event):
        fields = {
            key: str(getattr(component, key, "") or "")
            for key in ("file", "url", "path", "_type")
        }
        parts.append(" ".join(f"{key}={value}" for key, value in fields.items() if value))
    return " / ".join(part for part in parts if part)


def _is_forwarded(event: Any) -> bool:
    """这条消息是不是「合并转发」。"""

    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ in ("Forward", "Node", "Nodes"):
            return True
        if type(component).__name__ == "Json" and _multimsg_text(component):
            return True
    text = str(getattr(message_obj, "message_str", "") or "")
    return "[合并转发]" in text or "[聊天记录]" in text


# ---------------- 合并转发：读内容 → 交给多模态模型压成摘要 ----------------

MAX_FORWARD_NODES = 60
"""一条转发最多读多少条消息（再多也没人在意，摘要也放不下）。"""

MAX_FORWARD_IMAGES = 6
"""转发里的图最多送几张给多模态模型。"""

MAX_FORWARD_TEXT_CHARS = 1500
"""送进摘要提示词的原文上限（防一条转发把上下文撑爆）。"""

_FORWARD_PLACEHOLDER_RE = re.compile(
    r"[\[【(（]\s*(?:合并转发|转发消息|转发|聊天记录|message\s*record|forward(?:\s*message)?)\s*[\]】)）]",
    re.IGNORECASE,
)
"""渲染出来的转发占位符：摘要替换正文时要把这些清掉。"""


def _multimsg_text(component: Any) -> str:
    """QQ 的「合并转发」有时是一个 multimsg JSON：把里面的条目读成文本。"""

    data = getattr(component, "data", None)
    if isinstance(data, str):
        try:
            data = json.loads(data.replace("&#44;", ","))
        except Exception:
            return ""
    return _multimsg_text_from(data)


def _multimsg_text_from(data: Any) -> str:
    """multimsg 的 JSON 负载（dict）→ 里面的条目文本。"""

    if not isinstance(data, dict):
        return ""
    if str(data.get("app") or "") != "com.tencent.multimsg":
        return ""
    meta = data.get("meta")
    detail = meta.get("detail") if isinstance(meta, dict) else None
    news = detail.get("news") if isinstance(detail, dict) else None
    if not isinstance(news, list):
        return ""
    lines: list[str] = []
    for item in news:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").replace("[图片]", "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _strip_forward_placeholders(text: str) -> str:
    """去掉正文里的「[转发消息]」这类占位符，剩下的才是对方自己写的话。"""

    cleaned = _FORWARD_PLACEHOLDER_RE.sub(" ", str(text or ""))
    return " ".join(cleaned.split()).strip()


def _component_to_segment(component: Any) -> dict[str, Any] | None:
    """AstrBot 的消息组件 → OneBot 那样的 ``{"type":…, "data":…}``。

    转发内容的两种来源（协议端拉回来的 JSON、AstrBot 已经解析好的组件）
    归一成同一种写法，后面只需要一套走法。
    """

    if isinstance(component, dict):
        return component
    name = type(component).__name__
    if name == "Plain":
        return {"type": "text", "data": {"text": str(getattr(component, "text", "") or "")}}
    if name == "Image":
        ref = _raw_image_ref(component)
        return {"type": "image", "data": {"file": ref, "url": ref}}
    if name == "At":
        return {
            "type": "at",
            "data": {
                "name": str(getattr(component, "name", "") or ""),
                "qq": str(getattr(component, "qq", "") or ""),
            },
        }
    if name == "Face":
        return {"type": "face", "data": {}}
    if name == "File":
        return {"type": "file", "data": {"name": str(getattr(component, "name", "") or "")}}
    if name == "Json":
        return {"type": "json", "data": {"data": getattr(component, "data", "")}}
    if name:
        return {"type": name.lower(), "data": {}}
    return None


def _as_node(item: Any) -> dict[str, Any]:
    """AstrBot 的 Node 组件 / OneBot 的节点字典 → 统一的节点字典。"""

    if isinstance(item, dict):
        return item
    name = type(item).__name__
    if name == "Node":
        sender = str(getattr(item, "name", "") or getattr(item, "uin", "") or "").strip()
        content = list(getattr(item, "content", None) or [])
        return {
            "sender": {"nickname": sender},
            "content": [
                segment for segment in (_component_to_segment(c) for c in content)
                if segment is not None
            ],
        }
    return {}


def _node_list(value: Any) -> list[dict[str, Any]]:
    """把各种形态的转发内容归一成节点列表。"""

    if isinstance(value, dict):
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        nodes = (
            data.get("messages")
            or data.get("message")
            or data.get("nodes")
            or data.get("nodeList")
        )
        if isinstance(nodes, list):
            return [_as_node(item) for item in nodes if _as_node(item)]
        return [_as_node(value)] if _as_node(value) else []
    if isinstance(value, (list, tuple)):
        return [_as_node(item) for item in value if _as_node(item)]
    return []


def _node_segments(node: dict[str, Any]) -> list[dict[str, Any]]:
    """节点的内容（可能是段列表，也可能是一段 JSON 文本）。"""

    raw = node.get("message") or node.get("content") or []
    if isinstance(raw, str):
        body = raw.strip()
        if not body:
            return []
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
        else:
            raw = [{"type": "text", "data": {"text": body}}]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _collect_forward(
    nodes: list[dict[str, Any]],
    *,
    out_text: list[str],
    out_images: list[str],
    out_ids: list[str],
    depth: int = 0,
) -> None:
    """把节点列表摊成「谁说了什么」+ 图片地址（嵌套转发只记 id，等着再去拉）。"""

    if depth > 3:
        return
    for node in nodes:
        if len(out_text) >= MAX_FORWARD_NODES:
            return
        sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
        who = str(
            sender.get("nickname")
            or sender.get("card")
            or sender.get("user_id")
            or node.get("name")
            or node.get("uin")
            or ""
        ).strip()
        parts: list[str] = []
        for segment in _node_segments(node):
            kind = str(segment.get("type") or "").lower()
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind in ("text", "plain"):
                parts.append(str(data.get("text") or ""))
            elif kind == "image":
                ref = str(data.get("url") or data.get("file") or "").strip()
                if ref:
                    out_images.append(ref)
                    parts.append(f"［图片{len(out_images)}］")
                else:
                    parts.append("［图片］")
            elif kind == "at":
                parts.append(f"@{str(data.get('name') or data.get('qq') or '').strip()}")
            elif kind == "file":
                parts.append(f"［文件:{str(data.get('name') or data.get('file') or '').strip()}］")
            elif kind == "face":
                parts.append("［表情］")
            elif kind == "video":
                parts.append("［视频］")
            elif kind == "record":
                parts.append("［语音］")
            elif kind in ("forward", "forward_msg"):
                fid = data.get("id") or data.get("message_id")
                if fid:
                    out_ids.append(str(fid))
                    parts.append("［里面还有一条转发］")
                else:
                    _collect_forward(
                        _node_list(data.get("content")),
                        out_text=out_text,
                        out_images=out_images,
                        out_ids=out_ids,
                        depth=depth + 1,
                    )
            elif kind == "json":
                nested = _multimsg_text_from(data.get("data"))
                if nested:
                    parts.append(nested)
            elif kind == "nodes":
                _collect_forward(
                    _node_list(data.get("content")),
                    out_text=out_text,
                    out_images=out_images,
                    out_ids=out_ids,
                    depth=depth + 1,
                )
        body = "".join(parts).strip()
        if body:
            out_text.append(f"{who}：{body}" if who else body)


async def _fetch_forward(event: Any, forward_id: str) -> dict[str, Any] | None:
    """去协议端把一条转发拉回来（拿不到就返回 None，调用方保持原样）。"""

    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        return None
    candidates: list[dict[str, Any]] = [{"message_id": forward_id}, {"id": forward_id}]
    if str(forward_id).isdigit():
        candidates.extend([{"message_id": int(forward_id)}, {"id": int(forward_id)}])
    for params in candidates:
        try:
            result = await call_action("get_forward_msg", **params)
        except Exception:
            continue
        if isinstance(result, dict):
            return result
    return None


async def _forward_digest(event: Any) -> tuple[str, list[str], list[str]]:
    """这条消息里的合并转发 → (原文摘要, 图片地址, 转发 id)。

    三种形态都认：自带内容的 Node/Nodes、只给了 id 的 Forward（去协议端拉一次）、
    QQ 的 multimsg JSON。读不出来就返回空串，调用方保持原来的那句提示。
    """

    forward_ids: list[str] = []
    nodes: list[dict[str, Any]] = []
    multimsg: list[str] = []
    message_obj = getattr(event, "message_obj", None)
    for component in list(getattr(message_obj, "message", []) or []):
        name = type(component).__name__
        if name == "Forward":
            fid = str(getattr(component, "id", "") or "").strip()
            if fid:
                forward_ids.append(fid)
        elif name == "Node":
            nodes.extend(_node_list(component))
        elif name == "Nodes":
            nodes.extend(_node_list(list(getattr(component, "nodes", None) or [])))
        elif name == "Json":
            text = _multimsg_text(component)
            if text:
                multimsg.append(text)
    for fid in list(forward_ids):
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        nodes.extend(_node_list(payload))
    out_text: list[str] = []
    out_images: list[str] = []
    nested_ids: list[str] = []
    _collect_forward(nodes, out_text=out_text, out_images=out_images, out_ids=nested_ids)
    for fid in nested_ids:
        payload = await _fetch_forward(event, fid)
        if payload is None:
            continue
        _collect_forward(
            _node_list(payload), out_text=out_text, out_images=out_images, out_ids=[]
        )
    if not out_text and not out_images and multimsg:
        out_text = list(multimsg)
    digest = "\n".join(out_text).strip()[:MAX_FORWARD_TEXT_CHARS]
    return digest, out_images, forward_ids


def _forward_fingerprint(forward_ids: list[str], digest: str, images: list[str]) -> str:
    """同一条转发的指纹：协议端给了 id 就用 id，否则按内容算。"""

    ids = sorted({str(item).strip() for item in forward_ids if str(item).strip()})
    key = "|".join(ids) if ids else f"{digest}#{'|'.join(images)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _clean_forward_summary(text: str, limit: int) -> str:
    """转发摘要的收尾：压成一行、去掉模型爱加的「摘要：」开头、按配置截断。"""

    body = " ".join(str(text or "").split()).strip()
    if not body:
        return ""
    for prefix in ("摘要：", "摘要:", "内容摘要：", "内容摘要:", "总结：", "总结:"):
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    body = body.strip("「」“”\"'` ")
    if not body:
        return ""
    if len(body) > limit:
        body = body[: max(1, limit - 1)].rstrip() + "…"
    return body


def _with_forward_summary(text: str, summary: str) -> str:
    """把转发摘要拼进正文：清掉占位符，摘要跟在对方自己写的话后面。"""

    base = _strip_forward_placeholders(text)
    body = f"{FORWARD_SUMMARY_MARK}{summary}"
    return f"{base}\n{body}" if base else body


def _quoted_parts(event: Any) -> tuple[str, str]:
    """这条消息引用了谁说的什么：返回 ``(谁, 内容)``；没有引用就返回两个空串。

    两件容易漏的事：

    - 引用的那条可能**只有图片**（她刚发的照片），这时 ``message_str`` 是空的，
      只照着文本读会得到空串——她会完全不知道对方在指什么；
    - 引用的那条可能就是**她自己**发的：昵称她并不认识，要明确写成「你自己」。
    """

    message_obj = getattr(event, "message_obj", None)
    self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
    for component in list(getattr(message_obj, "message", []) or []):
        if type(component).__name__ != "Reply":
            continue
        text = " ".join(str(getattr(component, "message_str", "") or "").split())
        if not text:
            text = _chain_plain_text(component)
        if not text:
            continue
        sender_id = str(getattr(component, "sender_id", "") or "").strip()
        who = str(getattr(component, "sender_nickname", "") or "").strip()
        if self_id and sender_id and sender_id == self_id:
            who = "你自己"
        return who, text
    return "", ""


def _quoted_text(event: Any) -> str:
    """引用消息压成一行（``小明：晚上吃鱼``）：给看图那一步当背景用。"""

    who, text = _quoted_parts(event)
    if not text:
        return ""
    return f"{who}：{text}" if who else text


def _chain_plain_text(component: Any) -> str:
    """把被引用消息的消息链压成一行（图片写［图片］，表情写［表情］）。"""

    parts: list[str] = []
    for inner in list(getattr(component, "chain", None) or []):
        if _component_named(inner, "Plain"):
            text = str(getattr(inner, "text", "") or "").strip()
            if text:
                parts.append(text)
        elif _component_named(inner, "Image"):
            parts.append("［图片］")
        elif _component_named(inner, "Face"):
            parts.append("［表情］")
        elif _component_named(inner, "At"):
            name = str(getattr(inner, "name", "") or getattr(inner, "qq", "") or "").strip()
            if name:
                parts.append(f"@{name}")
    return " ".join(" ".join(part.split()) for part in parts if part)
