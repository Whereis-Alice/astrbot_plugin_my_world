"""Prepare image delivery identities without changing the native request."""
from __future__ import annotations

from typing import Any

from ..core.engine.constants import PENDING_IMAGE_KEEP


async def prepare_reply_images(
    plugin,
    event: Any,
    sources: list[str],
    extra: list[str] | None = None,
    excluded: set[str] | None = None,
    native: bool = False,
) -> tuple[list[str], dict[str, str], list[str]]:
    """这一轮直接交给模型看的图、它们的编号（``图1``/``图2``…），以及要转成文字的那几张。

    **规则**：发送范围（这次真的会进提示词的聊天记录）里**最近的 ``image_max`` 张图**
    都交给主模型看——不管它是这一条消息带的、还是前面几条带的，按时间序编号 图1/图2/…，
    和附件顺序一一对应；比它们更旧、但仍在发送范围内的图只以文字转述出现
    （被压成概览的老图压根不在这个范围里，不用管）。

    - 主模型看不见图时：这几张全部转述成文字（一次打包），不附图；
    - **没配转述模型**时：主模型看不看得见都照样附图（否则这些图就彻底没了）；
    - 已经转述过的图这次被附上了：它的转述不再渲染出来（见
      ``engine.attach_image_captions`` 与 ``prompt._render_rows``）。

    转述只发生一次：本来就要交给主模型看的图不会再转述一遍（白花钱）。

    ``extra`` 是别的插件往这一轮请求里加的图（``req.image_urls``）：和这一条消息
    自己带的图一样对待，去重之后一起编号——那是别人已经认可的"要给模型看"的图，
    丢掉就等于这一轮少看了一张。

    ``native``：注入模式只对 AstrBot 实际保留的附件编号，图片准备和转述
    继续由宿主与媒体插件处理；不把旧版挑选但未附上的历史图标成实际附件。
    """

    excluded = excluded or set()
    if native:
        # Inject mode retains the host's actual image inputs. The legacy
        # history-image selection below is not delivered by this route.
        actual = list(dict.fromkeys(str(ref).strip() for ref in extra or [] if str(ref).strip()))
        marks = {ref: f"图{index}" for index, ref in enumerate(actual, 1) if ref not in excluded}
        await plugin.engine.take_pending_images(event.unified_msg_origin)
        return [ref for ref in actual if ref not in excluded], marks, []
    session_id = event.unified_msg_origin
    context = plugin.engine.world.context
    cap = max(1, int(context.image_max))
    vision = getattr(plugin, "vision", None)
    captioned = bool(vision is not None and vision.enabled)
    mode = str(getattr(context, "chat_image_inline", "auto") or "auto").lower()
    inline = await plugin._chat_images_inline(session_id)
    pending = await plugin.engine.take_pending_images(session_id)
    # 按时间排好（先来的在前）：被搭话的那几条消息里的图排前面，最新那张在最后
    own = list(
        dict.fromkeys(
            [
                str(ref).strip()
                for ref in [*pending, *sources, *list(extra or [])]
                if str(ref).strip() and str(ref).strip() not in excluded
            ]
        )
    )
    # 「发送范围里」的图 = 这次真的会进提示词的聊天记录里的图（被压成概览的早就不在这里了，
    # 那种不用管）。取其中**最近的 ``image_max`` 张**交给主模型看，**不管它是这一条消息带的
    # 还是前面几条带的**——编号按时间序 图1/图2/…，和附件顺序一一对应。
    window = await plugin.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
    all_urls = list(
        dict.fromkeys(
            [
                *[
                    str(item.get("url") or "").strip()
                    for item in window
                    if str(item.get("url") or "").strip() and str(item.get("url") or "").strip() not in excluded
                ],
                *own,
            ]
        )
    )
    # 附件 = 这些图里**最近的 cap 张**（`all_urls` 已经是时间序），编号按同一顺序：
    # 第 i 张就是 图i，和附件顺序一一对应。
    # 显式关掉（``never``）就一张都不附；检测不出主模型能不能看图、又没配转述模型时，
    # 照样附过去（否则这些图就彻底没人看了）
    attach = bool(inline) or (not captioned and mode != "never")
    refs: list[str] = list(all_urls[-cap:]) if attach else []
    marks: dict[str, str] = {
        url: f"图{index}" for index, url in enumerate(refs, start=1)
    }
    notes: list[str] = []
    if captioned:
        dropped = [
            url for url in all_urls if url and url not in set(refs)
        ]
        notes = await plugin._caption_old_images(event, dropped, sources=sources)
    if not refs:
        return [], {}, notes
    detail = f"把 {len(refs)} 张图交给多模态主模型"
    if marks:
        detail += f"（聊天记录里的 {len(marks)} 张已编号）"
    await plugin.engine.note_vision(
        session_id, ok=True, images=len(refs), detail=detail
    )
    return refs, marks, notes
