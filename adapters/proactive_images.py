"""Request-owned model images using AstrBot's supported image preparation API."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path


class PreparedImages:
    """Normalize at most twelve refs, once each, within a shared time budget."""

    def __init__(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="my-world-media-")
        self.directory = Path(self.workspace.name)
        self.cache: dict[str, str | None] = {}
        self.montages: set[str] = set()
        self.remaining_seconds = 30.0

    async def prepare(self, refs: list[str]) -> tuple[list[str], bool]:
        images, failed = [], False
        for ref in dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref.strip()):
            if ref not in self.cache:
                if len(self.cache) >= 12:
                    failed = True
                    continue
                path = None
                if self.remaining_seconds > 0:
                    started = time.monotonic()
                    try:
                        from astrbot.core.utils.media_utils import prepare_model_image
                        result = await asyncio.wait_for(prepare_model_image(
                            ref, max_size=1536, output_dir=self.directory,
                        ), timeout=min(10, self.remaining_seconds))
                        # New SDKs return (path, montage, owned_preview, original).
                        # Only the image path is provider input. Compliant local
                        # originals remain outside our workspace and are preserved.
                        candidate = result[0] if isinstance(result, tuple) and len(result) == 4 else result
                        if isinstance(candidate, str) and candidate.strip():
                            path = candidate
                            if isinstance(result, tuple) and result[1] is True:
                                self.montages.add(path)
                    except Exception:
                        # Never send the rejected original after a failed conversion.
                        # CancelledError still propagates to release the request.
                        pass
                    finally:
                        self.remaining_seconds -= max(0, time.monotonic() - started)
                self.cache[ref] = path
            if self.cache[ref]:
                images.append(self.cache[ref])
            else:
                failed = True
        return list(dict.fromkeys(images)), failed

    def animation_note(self, refs: list[str]) -> str:
        return ("动态图片已按时间顺序采样拼成预览；各格是同一动画的不同时刻，"
                "不要把它们当作同时发生的画面。") if self.montages.intersection(refs) else ""

    def cache_sources(self, refs: list[str]) -> list[str]:
        originals = {path: source for source, path in self.cache.items() if path}
        return ["model-image-v1:" + originals.get(ref, ref) for ref in refs]

    def close(self) -> None:
        self.workspace.cleanup()
