"""Bounded media from the exact observed messages, with Motion Vision leases."""
from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from .proactive_images import PreparedImages


def part_value(part, name, default=None):
    return part.get(name, default) if isinstance(part, dict) else getattr(part, name, default)


def snapshot_event(event):
    snapshot = copy.copy(event)
    snapshot.message_obj = copy.deepcopy(event.message_obj)
    # Leave transport handles attached, isolate mutable request state.
    for name in ("_extras", "_extra", "extra"):
        if isinstance(getattr(event, name, None), dict):
            setattr(snapshot, name, copy.deepcopy(getattr(event, name)))
    return snapshot


class ProactiveMedia:
    def __init__(self, plugin):
        self.plugin = plugin
        self.events = OrderedDict()

    def remember(self, event, identity):
        key = (str(event.unified_msg_origin), str(identity.get("message_id") or identity.get("event_id") or ""))
        if not key[1]:
            return
        try:
            snapshot = snapshot_event(event)
        except Exception:
            return
        self.events[key] = (time.monotonic(), snapshot)
        self.events.move_to_end(key)
        while self.events and (len(self.events) > 256 or time.monotonic() - next(iter(self.events.values()))[0] > 900):
            self.events.popitem(last=False)

    def _motion(self, session_id):
        context = self.plugin.context
        try:
            allowed = context.get_config(session_id).get("plugin_set", ["*"])
            for star in context.get_all_stars() or []:
                names = {str(getattr(star, name, "")) for name in ("name", "root_dir_name")}
                if ("astrbot_plugin_motion_vision" in names and getattr(star, "activated", False)
                        and ("*" in allowed or names.intersection(allowed))):
                    instance = getattr(star, "star_cls", None)
                    if callable(getattr(instance, "handle", None)) and callable(getattr(instance, "release_media_request", None)):
                        return instance
        except Exception:
            pass
        return None

    @asynccontextmanager
    async def context(self, session_id, rows, *, images=None):
        # Only the selected conversation is eligible. Two media-bearing messages
        # per decision avoid decoding a full chat archive on each opportunity.
        selected = [row for row in rows if not row.get("is_self")
                    and str(row.get("origin") or session_id) == session_id
                    and (row.get("images") or row.get("has_images"))][-2:]
        extra_images = [ref for ref in images or [] if isinstance(ref, str) and ref.strip()]
        if not selected and not extra_images:
            yield {"text": "", "images": []}
            return
        from astrbot.api.provider import ProviderRequest
        from ..core.engine.life_inference import released_life_lock
        images, notes, leases, consumed = [], [], [], set()
        prepared = PreparedImages()
        try:
            async with released_life_lock(self.plugin.engine):
                motion = self._motion(session_id)
                for row in selected:
                    key = (session_id, str(row.get("message_id") or row.get("event_id") or ""))
                    cached = self.events.get(key)
                    sources = [str(x) for x in row.get("images", []) if isinstance(x, str)][:4]
                    consumed.update(sources)
                    request = ProviderRequest(prompt=str(row.get("reading_text") or row.get("text") or "看看消息里的图片"),
                                              image_urls=list(sources), contexts=[])
                    if motion and cached and time.monotonic() - cached[0] <= 900:
                        event = snapshot_event(cached[1])
                        leases.append((motion, event))
                        try:
                            await asyncio.wait_for(motion.handle(event, request), timeout=30)
                        except asyncio.TimeoutError:
                            notes.append("这条消息的动态图片未能及时解析，不猜测其内容。")
                        except Exception:
                            notes.append("这条消息的动态图片暂时无法解析。")
                    refs = list(request.image_urls or [])
                    texts = []
                    for part in getattr(request, "extra_user_content_parts", []) or []:
                        text = part_value(part, "text", "")
                        if isinstance(text, str) and text:
                            texts.append(text[:2000])
                        url = part_value(part_value(part, "image_url", {}), "url", "")
                        if isinstance(url, str) and url:
                            refs.append(url)
                    refs, failed = await prepared.prepare(list(dict.fromkeys(refs))[:8])
                    if failed:
                        texts.append("部分图片未能读取，不能据此推测画面或动作。")
                    animation_note = prepared.animation_note(refs)
                    if animation_note:
                        texts.append(animation_note)
                    vision = getattr(self.plugin, "vision", None)
                    if refs and vision is not None and vision.enabled:
                        try:
                            captions = await asyncio.wait_for(vision.describe(refs,
                                question="\n".join(text for text in (str(row.get("text") or ""), animation_note) if text),
                                cache_sources=prepared.cache_sources(refs)), timeout=30)
                            usable = [str(caption).strip()[:500] for caption in captions or [] if str(caption or "").strip()]
                            texts.extend(usable)
                            if len(usable) < len(refs):
                                texts.append("部分图片没有取得有效转述，不猜测未看见的画面。")
                        except (asyncio.TimeoutError, Exception):
                            texts.append("图片转述暂时不可用，不猜测未看见的画面。")
                        refs = []
                    if texts or refs:
                        notes.append("消息 " + key[1] + "（" + str(row.get("name") or "群友") + "）的媒体：\n" + "\n".join(texts))
                        images.extend(refs)
                # Tool-returned images also bypass the native reply preprocessing.
                # Don't re-add originals that Motion Vision already replaced.
                extra, failed = await prepared.prepare([ref for ref in extra_images if ref not in consumed])
                images.extend(extra)
                if animation_note := prepared.animation_note(extra):
                    notes.append(animation_note)
                if failed:
                    notes.append("部分工具图片未能读取，不能据此推测画面或动作。")
            yield {"text": "\n\n".join(notes)[:6000], "images": list(dict.fromkeys(images))[:12]}
        finally:
            try:
                for motion, event in leases:
                    try:
                        await motion.release_media_request(event, None)
                    except Exception:
                        pass
            finally:
                prepared.close()

    def close(self):
        self.events.clear()
