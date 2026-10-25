"""图片转述适配：多模态看图、转述缓存、合并转发摘要。
"""

from __future__ import annotations

from ..core.model_call_runtime import tracked_generate

import asyncio
import hashlib
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from ..core.defaults import (
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_CAPTION_RELATION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
)
from ..core.message_parts import MAX_FORWARD_IMAGES, _clean_forward_summary

if TYPE_CHECKING:
    from .. import MyWorldPlugin


CAPTION_SYSTEM_PROMPT = DEFAULT_CAPTION_PROMPT
"""看图提示词的内置默认值；「全局设置 → 图片转述」里可以改成自己的一套。"""

CAPTION_RELATION_PROMPT = DEFAULT_CAPTION_RELATION_PROMPT
"""关系提示词的内置默认值；同样可以在「全局设置 → 图片转述」里改。"""

# 多图合并成一次调用时，要模型按「图1：…」逐行输出；解析不出来就退回逐张。
_MULTI_IMAGE_RULE = (
    "\n\n这次一次给你 {count} 张图：每张图各一行，"
    "以「图1：」「图2：」这样开头，顺序和图片顺序一致，不要合并成一行。"
)


def _split_multi_caption(text: str, count: int) -> list[str]:
    """把「图1：… 图2：…」拆成逐图结果；对不上就返回空列表（调用方退回逐张）。"""

    picked = {}
    for line in str(text or "").splitlines():
        match = re.match(r'^\s*(?:图\s*)?(\d+)\s*[：:.．)）]\s*(.*?)\s*$', line)
        if not match:
            continue
        index, body = int(match[1]), match[2].strip()
        if not 1 <= index <= count or index in picked or not body:
            return []
        picked[index] = body
    return [picked[i] for i in range(1, count + 1)] if len(picked) == count else []

# 「与话题的关系」之后的内容是会过期的：换个话题再说同一张图，这半句就不对了。
# 命中缓存时只复用前面的画面/类型描述，关系交回主模型判断。
_CAPTION_RELATION_MARKERS = ("与话题的关系", "和话题的关系", "与当前话题", "关系：")


def _caption_prefix(caption: str) -> str:
    """从一整条转述里切出"与话题无关"的那半句。切不出来就整条返回。"""

    text = " ".join(str(caption or "").split())
    if not text:
        return ""
    cut = len(text)
    for marker in _CAPTION_RELATION_MARKERS:
        index = text.find(marker)
        if index > 0:
            cut = min(cut, index)
    prefix = text[:cut].strip(" ｜|，,。;；")
    return prefix or text


def _images_head(count: int) -> str:
    """这条消息带了几张图（收图那一步写进留档的中性说法）。

    交付决策在准备回复时才做（`_reply_images`），所以这里**不写编号、也不承诺**
    "会一起发给你"——那一步会再给一句带编号的说明。
    """

    total = max(0, int(count or 0))
    if total <= 0:
        return ""
    return f"这条消息带了 {total} 张图片"


def _strip_image_index(text: str) -> str:
    """剥掉模型自己加的「图1：」「图 1：」「1.」这类编号前缀。"""

    body = str(text or "").strip()
    for _ in range(2):
        match = re.match(
            r"^(?:图\s*[0-9一二三四五六七八九十]+|[0-9]+|[一二三四五六七八九十]+)\s*[：:.、)）]\s*",
            body,
        )
        if not match:
            break
        body = body[match.end() :].strip()
    return body


def _image_fingerprint(source: str) -> str:
    """图片缓存指纹：内容型来源可复用，URL 保留影响内容的参数。

    - ``base64://`` 直接对内容算 sha1（协议端把图转成 base64 时最可靠）；
    - http(s) 保留 query：file、id、尺寸等参数可能决定图片内容；
    - 其它（本地文件、协议端给的 md5 名）原样取用。
    """

    raw = str(source or "").strip()
    if not raw:
        return ""
    digest = ""
    if raw.startswith("base64://"):
        payload = raw[len("base64://") :]
        digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith("data:"):
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    elif raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(raw)
        digest = hashlib.sha1(
            urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")).encode("utf-8", "ignore")
        ).hexdigest()
        # Old batch captions could be cached against the wrong image.
        return f"img:caption3:url:{digest}"
    else:
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()
    return f"img:caption3:{digest}"


class AstrBotVision:
    """把图片转成一段文字，让不带视觉能力的模型也能"看到"。"""

    def __init__(self, plugin: "MyWorldPlugin", provider_id: str = "") -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self._negative: dict[str, float] = {}
        """失败过的指纹 → 时间：短时内不再重试，但**不**写进持久缓存。"""

        self._inflight: dict[str, asyncio.Future] = {}
        """同一张图正在识别时，其它消息先等它——不然一张图会被认好几遍。"""

        self.hits = 0
        """本次运行命中了多少次缓存（省下的转述调用）。"""

        self.last_error: str = ""
        """最近一次转述失败的原因（写进日志，方便排查"模型看不见图片"）。"""

    @property
    def enabled(self) -> bool:
        return bool(self.provider_id.strip())

    def _system_prompt(self) -> str:
        """当前生效的**看图**提示词：全局设置里填了就用它，没填用内置默认。"""

        try:
            prompt = str(self.plugin.engine.world.vision.prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or CAPTION_SYSTEM_PROMPT

    def _relation_prompt(self) -> str:
        """当前生效的**关系**提示词。"""

        try:
            prompt = str(self.plugin.engine.world.vision.relation_prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or CAPTION_RELATION_PROMPT

    def _relation_enabled(self) -> bool:
        try:
            return bool(getattr(self.plugin.engine.world.vision, "relation_enabled", True))
        except Exception:
            return True

    async def describe(
        self,
        image_sources: list[str],
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
        cache_sources: list[str] | None = None,
    ) -> list[str]:
        """把图片逐个转述成文字。失败或未配置时返回空串（调用方自己降级）。

        分两步：**看图**（多模态，多张图合并成一次，按图片指纹缓存）
        → **关系**（纯文本、不带图、每次现算）。最终拼成
        `画面描述｜类型｜文字｜与话题的关系：…`，对下游完全兼容。
        """

        scene = self._scene_text(question=question, quoted=quoted, context_lines=context_lines)
        # **看图那一步不带话题上下文**：把"最近群里在聊什么"塞给它，弱一点的模型
        # 会照着话题脑补画面（实测：一张"口蘑"表情包被描述成"群聊截图、两个人在
        # 争论蘑菇是不是植物"）。提示词里本来就写着"不要写这张图和话题的关系——
        # 那部分由另一段提示词单独生成"，把上下文提前给它等于自相矛盾。
        # 话题相关性交给下一步「关系」——它本来就是为这个准备的，而且不带图。
        looks = await self._look_many(image_sources, "", cache_sources=cache_sources)
        if self._relation_enabled() and any(item for item in looks):
            relations = await self._relate(looks, scene)
        else:
            relations = ["" for _ in looks]
        return [
            self._combine(look, relation)
            for look, relation in zip(looks, relations)
        ]

    @staticmethod
    def _combine(look: str, relation: str) -> str:
        """把「画面｜类型｜文字」和「与话题的关系：…」拼成一条。"""

        text = " ".join(str(look or "").split())
        extra = " ".join(str(relation or "").split())
        if not text:
            return ""
        # 关系那一步经常顺手带上「图1：」（提示词里给了编号的样例），
        # 先剥掉编号再判断，免得拼出「与话题的关系：图1：与话题的关系：…」
        extra = _strip_image_index(extra)
        if not extra:
            return text
        for marker in _CAPTION_RELATION_MARKERS:
            if extra.startswith(marker):
                body = extra[len(marker) :].lstrip("：: ｜|")
                return f"{text}｜与话题的关系：{body}" if body else text
        return f"{text}｜与话题的关系：{extra}"

    async def _look_many(self, image_sources: list[str], scene: str, *, cache_sources: list[str] | None = None) -> list[str]:
        """看图：命中的走缓存，未命中的合并成一次调用（失败退回逐张）。"""

        results: list[str] = ["" for _ in image_sources]
        # Prepared request files have new paths each time; cache their source
        # identity while sending only the normalized working file to the model.
        keys = cache_sources if cache_sources is not None and len(cache_sources) == len(image_sources) else image_sources
        pending: list[int] = []
        for index, source in enumerate(image_sources):
            if not source:
                continue
            cached = await self._cached_look(keys[index])
            if cached:
                results[index] = cached
                continue
            pending.append(index)
        if not pending:
            return results
        if not self.enabled:
            return results
        if len(pending) == 1:
            index = pending[0]
            results[index] = await self._describe_cached(image_sources[index], scene, cache_source=keys[index])
            return results
        # 多张一起看：一次多模态调用
        sources = [image_sources[index] for index in pending]
        merged = await self._describe_batch(sources, scene)
        for slot, index in enumerate(pending):
            caption = merged[slot] if slot < len(merged) else ""
            if not caption:
                # 合并解析失败：这张退回逐张调用，稳妥优先
                caption = await self._describe_cached(image_sources[index], scene, cache_source=keys[index])
            else:
                await self._store_look(keys[index], caption)
            results[index] = caption
        return results

    async def describe_to_text(self, image_sources: list[str], prompt: str = "") -> str:
        """给引擎用的看图入口（天气工具返回的图要读出来）：带缓存、多图合并成一次调用。"""

        sources = [str(item) for item in (image_sources or []) if str(item).strip()]
        if not sources or not self.enabled:
            return ""
        looks = await self._look_many(sources, str(prompt or "").strip())
        return "\n".join(text for text in looks if text).strip()

    async def summarize_forward(self, digest: str, images: list[str], *, fingerprint: str) -> str:
        """把一段合并转发压成摘要（含里面的图）：带缓存，失败返回空串。

        调用方拿到空串时保持原来的做法（只在聊天记录里落一句"这是转发"）。
        """

        body = " ".join(str(digest or "").split())
        if not body or not self.enabled:
            return ""
        cached = await self._cached_forward(fingerprint)
        if cached:
            return cached
        refs = [
            str(item).strip()
            for item in list(images or [])[:MAX_FORWARD_IMAGES]
            if str(item).strip()
        ]
        try:
            response = await tracked_generate(self.plugin, purpose="forward", reason="转发消息摘要", role="vision",
                chat_provider_id=self.provider_id,
                system_prompt=self._forward_prompt(),
                prompt=f"这段转发的内容：\n{body}",
                image_urls=refs or None,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 转发摘要失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "摘要模型没有返回文字"
            return ""
        summary = _clean_forward_summary(text, self._forward_max_chars())
        if not summary:
            return ""
        await self._store_forward(fingerprint, summary)
        return summary

    def _forward_prompt(self) -> str:
        """当前生效的转发摘要提示词：设置里填了就用它，没填用内置默认。"""

        try:
            prompt = str(self.plugin.engine.world.vision.forward_prompt or "").strip()
        except Exception:
            prompt = ""
        return prompt or DEFAULT_FORWARD_PROMPT

    def _forward_max_chars(self) -> int:
        try:
            limit = int(self.plugin.engine.world.vision.forward_max_chars or 0)
        except Exception:
            limit = 0
        return max(80, limit or 300)

    async def _cached_forward(self, fingerprint: str) -> str:
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get_forward(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        self.hits += 1
        try:
            await self.plugin.db.call("touch_forward_summary", fingerprint=fingerprint)
        except Exception:
            pass
        return str(entry.get("summary") or "")

    async def _store_forward(self, fingerprint: str, summary: str) -> None:
        if not fingerprint or not summary:
            return
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_forward_summary",
                fingerprint=fingerprint,
                summary=summary,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_forward_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 写转发缓存失败：{exc}")

    async def _cache_get_forward(
        self, fingerprint: str, max_age: float
    ) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_forward_summary", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    async def _cached_look(self, source: str) -> str:
        """命中缓存就返回"看图结果"（不含关系）。"""

        fingerprint = _image_fingerprint(source)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")):
            return ""
        entry = await self._cache_get(fingerprint, float(cache.get("max_age") or 0))
        if entry is None:
            return ""
        self.hits += 1
        await self._cache_touch(fingerprint)
        caption = str(entry.get("caption") or "")
        prefix = str(entry.get("prefix") or "")
        # 老行存的是含关系段的旧格式：只取"关系之前"的部分
        if "与话题的关系" in caption:
            return prefix or _caption_prefix(caption)
        return caption

    async def _store_look(self, source: str, caption: str) -> None:
        fingerprint = _image_fingerprint(source)
        cache = self._cache_config()
        if not fingerprint or not bool(cache.get("enabled")) or not caption:
            return
        await self._cache_put(fingerprint, caption=caption, prefix=caption)

    async def _describe_batch(self, sources: list[str], scene: str) -> list[str]:
        """一次看多张图：要求逐行输出，解析不出来返回空列表。"""

        if len(sources) < 2:
            return []
        prompt = (scene + "\n\n" if scene else "") + _MULTI_IMAGE_RULE.format(
            count=len(sources)
        ).strip()
        try:
            response = await tracked_generate(self.plugin, purpose="vision", reason="多张图片转述", role="vision",
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=prompt,
                image_urls=list(sources),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 多图转述失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
            return []
        return _split_multi_caption(text, len(sources))

    async def _relate(self, looks: list[str], scene: str) -> list[str]:
        """关系分析：纯文本、不带图；失败就返回空串（另一半照样能用）。"""

        lines = [f"图{index}：{text}" for index, text in enumerate(looks, start=1) if text]
        if not lines:
            return ["" for _ in looks]
        provider = self._relation_provider()
        prompt = (scene + "\n\n" if scene else "") + "图片转述：\n" + "\n".join(lines)
        try:
            response = await tracked_generate(self.plugin, purpose="vision_relation", reason="分析图片与当前话题的关系", role="vision",
                chat_provider_id=provider,
                system_prompt=self._relation_prompt(),
                prompt=prompt,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 图片关系分析失败：{exc}")
            return ["" for _ in looks]
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return ["" for _ in looks]
        if len(lines) == 1:
            # 只有一张图时它也可能顺手写「图1：」——剥掉，免得拼出双份标签
            single = _strip_image_index(text)
            return [single if len(looks) == 1 else "" for _ in looks]
        parts = _split_multi_caption(text, len(looks))
        if not parts:
            return ["" for _ in looks]
        return parts

    def _relation_provider(self) -> str:
        """关系分析用哪个模型：打杂模型（纯文本）；没配就跟随看图模型。"""

        configured = str(getattr(self.plugin, "utility_provider_id", "") or "").strip()
        return configured or self.provider_id

    async def _describe_cached(self, source: str, scene: str, *, cache_source: str | None = None) -> str:
        """看单张图（缓存由调用方先查过）：带并发去重、失败不落盘。"""

        if not source:
            return ""
        cache_source = source if cache_source is None else cache_source
        fingerprint = _image_fingerprint(cache_source)
        if not self.enabled:
            return ""
        # 同一张图正在识别：等它，别再调一次模型
        pending = self._inflight.get(fingerprint) if fingerprint else None
        if pending is not None:
            try:
                return str(await asyncio.shield(pending))
            except Exception:
                return ""
        failed_at = float(self._negative.get(fingerprint) or 0.0)
        if failed_at and time.time() - failed_at < 60:
            return ""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if fingerprint:
            self._inflight[fingerprint] = future
        try:
            caption = await self._describe_one(source, scene)
            if not caption:
                # Failed perception is transient, never a durable description.
                if fingerprint:
                    self._negative[fingerprint] = time.time()
                return ""
            await self._store_look(cache_source, caption)
            if not future.done():
                future.set_result(caption)
            return caption
        finally:
            if fingerprint:
                self._inflight.pop(fingerprint, None)
            # Owner cancellation or a storage exception must also release followers.
            if not future.done():
                future.set_result("")

    def _cache_config(self) -> dict[str, Any]:
        try:
            vision = self.plugin.engine.world.vision
            days = max(0, int(getattr(vision, "cache_days", 30) or 0))
            return {
                "enabled": bool(getattr(vision, "cache_enabled", True)),
                "max": max(1, int(getattr(vision, "cache_max", 500) or 500)),
                "max_age": days * 86400 if days else 0,
            }
        except Exception:
            return {"enabled": True, "max": 500, "max_age": 30 * 86400}

    async def _cache_get(self, fingerprint: str, max_age: float) -> dict[str, Any] | None:
        try:
            return await self.plugin.db.call(
                "get_image_caption", fingerprint=fingerprint, max_age_seconds=max_age
            )
        except Exception:
            return None

    async def _cache_touch(self, fingerprint: str) -> None:
        try:
            await self.plugin.db.call("touch_image_caption", fingerprint=fingerprint)
        except Exception:
            pass

    async def _cache_put(self, fingerprint: str, *, caption: str, prefix: str) -> None:
        cache = self._cache_config()
        try:
            await self.plugin.db.call(
                "put_image_caption",
                fingerprint=fingerprint,
                caption=caption,
                prefix=prefix,
                model=self.provider_id,
            )
            await self.plugin.db.call(
                "trim_image_cache",
                keep=int(cache.get("max") or 500),
                max_age_seconds=float(cache.get("max_age") or 0),
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 写图片缓存失败：{exc}")

    @staticmethod
    def _scene_text(
        *,
        question: str = "",
        quoted: str = "",
        context_lines: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        if question:
            parts.append(f"这条消息里说的话：{question}")
        if quoted:
            parts.append(f"引用的消息：{quoted}")
        lines = [str(item).strip() for item in (context_lines or []) if str(item).strip()]
        if lines:
            parts.append("最近群里在聊：\n" + "\n".join(lines[-6:]))
        return "\n".join(parts)

    async def _describe_one(self, source: str, scene: str) -> str:
        try:
            response = await tracked_generate(self.plugin, purpose="vision", reason="单张图片转述", role="vision",
                chat_provider_id=self.provider_id,
                system_prompt=self._system_prompt(),
                prompt=(scene + "\n\n" if scene else "") + "请描述这张图片。",
                image_urls=[source],
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 图片转述失败：{exc}")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            self.last_error = "转述模型没有返回文字（可能不支持图片输入）"
        return " ".join(text.split())[:200]


async def _caption_result_images(
    plugin: "MyWorldPlugin",
    images: list[str],
    *,
    scene: str = "",
) -> tuple[list[str], list[str]]:
    """工具 / 指令返回的图片：配了转述模型就先转述，没配就原样带回去。

    返回 ``(转述后的描述, 还需要交给多模态模型的图片)``——转述成功就不必再传图片，
    失败或没配转述模型时把地址原样交上去，由 ``llm_generate`` 那条路处理。
    """

    if not images:
        return [], []
    limit = 3
    try:
        limit = max(1, int(plugin.engine.world.context.image_max))
    except Exception:
        pass
    picked = images[:limit]
    vision = getattr(plugin, "vision", None)
    if vision is None or not vision.enabled:
        return [], picked
    try:
        captions = await vision.describe(picked, question=scene)
    except Exception as exc:
        plugin.logger.debug(f"[my_world] 返回内容里的图片转述失败：{exc}")
        captions = []
    described = [
        f"图片{index + 1}：{caption}"
        for index, caption in enumerate(captions or [])
        if caption
    ]
    if not described:
        return [], picked
    return described, []
