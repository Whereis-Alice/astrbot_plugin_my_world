"""临时把事件会话切到真正要跑的那条，跑完还原。
"""

from __future__ import annotations

from typing import Any


def _swap_event_session(event: Any, session_id: str):
    """临时把事件的会话换成真正要跑的那条，返回还原用的回调。

    指令处理器和工具（出图、发语音这类）常常按事件的会话把结果直接发出去。
    借来的事件如果还挂在别的会话上，结果就会跑错地方——私聊里让她拍照、
    图却发到群里就是这么来的。借同组事件时把它指到真正的落点上再跑，
    跑完立刻还原（事件还连着 AstrBot 的分发，不能一直留着改动）。
    """

    wanted = str(session_id or "").strip()
    previous = getattr(event, "session", None)
    previous_new = getattr(event, "new_session", None)
    try:
        current = str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        current = ""
    if not wanted or current == wanted:
        return lambda: None

    def restore() -> None:
        # 真实事件里 ``unified_msg_origin`` 是从 ``session`` 算出来的，换回
        # session 就够了；有的实现（以及测试替身）只是个普通属性，所以两边
        # 都还原一次，哪种都不会留下改过的会话。
        try:
            event.session = previous
        except Exception:
            pass
        try:
            event.new_session = previous_new
        except Exception:
            pass
        try:
            if str(getattr(event, "unified_msg_origin", "") or "") != current:
                event.unified_msg_origin = current
        except Exception:
            pass

    try:
        event.unified_msg_origin = wanted
    except Exception:
        return lambda: None
    return restore
