"""Validate proactive image inputs with AstrBot SDK; no provider or transport calls."""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image
from astrbot.core.utils.media_utils import resolve_image_ref_to_base64_data


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("world_media_sdk_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
ProactiveMedia = importlib.import_module(package.__name__ + ".adapters.proactive_media").ProactiveMedia


async def main():
    context = types.SimpleNamespace(get_config=lambda _: {"plugin_set": []}, get_all_stars=lambda: [])
    plugin = types.SimpleNamespace(context=context, engine=types.SimpleNamespace(), vision=None)
    media = ProactiveMedia(plugin)
    results = []
    with tempfile.TemporaryDirectory(prefix="world-media-sdk-") as temporary:
        root = Path(temporary)
        gif = root / "animation-disguised-as.jpg"
        frames = [Image.new("RGB", (24, 24), color) for color in ("red", "blue", "green")]
        try:
            frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
            frames[0].save(root / "static.png")
        finally:
            for frame in frames:
                frame.close()
        encoded = base64.b64encode(gif.read_bytes()).decode("ascii")
        sources = {
            "GIF hidden behind JPG extension": str(gif),
            "inline GIF": "data:image/gif;base64," + encoded,
            "base64 GIF": "base64://" + encoded,
            "static PNG": str(root / "static.png"),
        }
        for name, source in sources.items():
            original = Path(source) if not source.startswith(("data:", "base64://")) else None
            row = {"origin": "synthetic:GroupMessage:media", "message_id": "test",
                   "text": "合成媒体测试", "images": [source]}
            # No event snapshot: the fallback must handle the actual image bytes.
            async with media.context(row["origin"], [row]) as data:
                assert len(data["images"]) == 1, data["text"]
                path = Path(data["images"][0])
                assert path.is_file() and path != gif
                resolved = await resolve_image_ref_to_base64_data(str(path), strict=True)
                assert resolved.mime_type in {"image/jpeg", "image/png"}, resolved.mime_type
                with Image.open(path) as prepared:
                    assert getattr(prepared, "n_frames", 1) == 1
                    assert max(prepared.size) <= 1536
                results.append({"case": name, "passed": True, "model_mime": resolved.mime_type})
            if original is not None and path == original:
                assert original.exists(), "a compliant original reused by the SDK must be preserved"
            else:
                assert not path.exists(), "request preview must be released after consumption"
            assert gif.exists(), "original file must be preserved"
        async with media.context("synthetic:GroupMessage:media", [], images=[str(gif)]) as data:
            assert len(data["images"]) == 1
            assert data["images"][0] != str(gif)
        results.append({"case": "tool image without chat rows", "passed": True})
        bad = root / "bad.gif"
        bad.write_bytes(b"invalid image")
        async with media.context("synthetic:GroupMessage:media", [], images=[str(bad)]) as data:
            assert not data["images"] and "未能读取" in data["text"]
        results.append({"case": "corrupt image cannot reach model", "passed": True})
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
