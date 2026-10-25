"""Media presence and source labels without interpreting its contents."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from .message_parts import _raw_image_ref, _is_image_component


def video_components(event: Any) -> Iterator[tuple[Any, bool]]:
    for component in getattr(getattr(event, 'message_obj', None), 'message', []) or []:
        if type(component).__name__ == 'Video':
            yield component, False
        elif type(component).__name__ == 'Reply':
            for quoted in getattr(component, 'chain', []) or []:
                if type(quoted).__name__ == 'Video':
                    yield quoted, True


def has_videos(event: Any) -> bool:
    return any(video_components(event))


def video_note(event: Any) -> str:
    videos = list(video_components(event))
    if not videos:
        return ''
    source = '引用消息中有视频' if all(quoted for _, quoted in videos) else '这条消息附有视频'
    return source + '；视频内容须以本轮实际获得的解析为准，不能用其它消息的图片代替视频画面'


def image_origin_labels(event: Any, refs: Iterable[str], row: dict[str, Any],
                        *, unknown: str | None = None) -> dict[str, str]:
    base = f"会话 {row.get('origin', '')}，消息 {row.get('message_id') or row.get('event_id') or ''}（{row.get('name') or '群友'}）"
    labels = {}
    for component in getattr(getattr(event, 'message_obj', None), 'message', []) or []:
        if _is_image_component(component):
            labels.setdefault(_raw_image_ref(component), []).append(base + '本条发送的图片')
        elif type(component).__name__ == 'Reply':
            for quoted in getattr(component, 'chain', []) or []:
                if _is_image_component(quoted):
                    label = (base + '引用的图片，原消息 ' + str(getattr(component, 'id', '') or '')
                             + '（' + str(getattr(component, 'sender_nickname', '') or getattr(component, 'sender_id', '') or '原发送者') + '）')
                    labels.setdefault(_raw_image_ref(quoted), []).append(label)
    quoted = row.get('reply_to') or {}
    fallback = unknown or base + ('的媒体（含引用；未确认归属的图不能当作这次新发的图）' if quoted else '的图片')
    return {ref: '；'.join(dict.fromkeys(labels.get(ref, [fallback]))) for ref in refs}


def native_image_note(event: Any, marks: dict[str, str]) -> str:
    """Describe only actual host attachments; transformed paths do not prove ownership."""
    if not marks:
        return ''
    row = {'origin': event.unified_msg_origin,
           'message_id': getattr(event.message_obj, 'message_id', ''),
           'name': event.get_sender_name()}
    labels = image_origin_labels(event, marks, row,
        unknown='附件来源尚未确认，不能按位置猜它属于哪条消息或谁发的；以宿主及媒体插件实际提供的来源为准')
    return ('本轮实际图片附件的顺序与下面编号对应：\n'
            + '\n'.join(f'{mark}：{labels[ref]}' for ref, mark in marks.items())
            + '\n历史图片标记不代表本轮又附了原图；只依据实际画面或可靠转述判断内容，'
              '不能把其它消息的图片或聊天梗当成本图内容。内部编号不照读给群友。')
