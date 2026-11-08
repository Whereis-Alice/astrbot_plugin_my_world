"""Short-lived editor exports. Rendering here never touches the message transport."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import io
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4
import zipfile

import aiohttp

from ..adapters.visit_cards import VisitCardRenderer
from ..core.visits.protocol import digest

EXPORT_TTL = 600
EXPORT_TIMEOUT = 170
MAX_EXPORT_JOBS = 8
MAX_EXPORT_PENDING = 2
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EXPORT_BYTES = 128 * 1024 * 1024


async def _disk(function, *args):
    """Finish an in-flight disk operation before cancellation removes its directory."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@dataclass
class _Export:
    id: str
    scope: str
    visit_id: str
    fingerprint: str
    temporary: Any
    touched: float
    state: str = "pending"
    pages: list[dict] = field(default_factory=list)
    error: str = ""
    task: asyncio.Task | None = None


def _image_info(data: bytes) -> tuple[str, str, int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {"PNG", "JPEG"}:
            raise ValueError("渲染器没有返回可下载的 PNG 或 JPEG 图片")
        width, height = image.size
        image.verify()
        return (("png", "image/png") if image.format == "PNG" else ("jpg", "image/jpeg")) + (width, height)


async def _image_bytes(ref: str) -> bytes:
    if ref.startswith("base64://"):
        value = ref[len("base64://"):]
        if len(value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise ValueError("卡片图片太大，请缩短记录后重试")
        data = base64.b64decode(value, validate=True)
    elif ref.startswith(("https://", "http://")):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            async with session.get(ref) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ValueError("卡片图片太大，请缩短记录后重试")
                    chunks.append(chunk)
                data = b"".join(chunks)
    else:
        if ref.startswith("file://"):
            parsed = urlsplit(ref)
            if parsed.netloc and parsed.netloc != "localhost":
                raise ValueError("渲染图片路径不可用")
            path = Path(url2pathname(parsed.path))
        else:
            path = Path(ref)
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("卡片图片太大，请缩短记录后重试")
        data = await _disk(path.read_bytes)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("卡片图片太大，请缩短记录后重试")
    return data


class VisitCardExports:
    """One renderer and at most two pending jobs; exports expire after 10 minutes."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._jobs: dict[str, _Export] = {}
        self._gate = asyncio.Semaphore(1)
        self._closed = False
        self._janitor: asyncio.Task | None = None

    @staticmethod
    def _summary(job: _Export) -> dict:
        return {"job_id": job.id, "state": job.state,
                "pages": [{key: value for key, value in page.items() if key != "path"} for page in job.pages],
                "filename": f"my-world-visit-{job.id[:12]}.zip", "error": job.error}

    def _prune(self) -> None:
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if job.state != "pending" and now - job.touched >= EXPORT_TTL:
                job.temporary.cleanup()
                self._jobs.pop(key, None)

    async def _cleanup_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(60)
            self._prune()

    async def start(self, scope: str, visit_id: str) -> dict:
        if self._closed or getattr(self.plugin, "retired", False):
            raise ValueError("插件已重载，请刷新后重新生成")
        snapshot = await self.plugin.visits.record_card_snapshot(scope, visit_id)
        if self._closed or getattr(self.plugin, "retired", False):
            raise ValueError("插件已重载，请刷新后重新生成")
        self._prune()
        fingerprint = digest(snapshot)
        for key, job in list(self._jobs.items()):
            if job.scope == scope and job.visit_id == visit_id and job.state == "failed":
                job.temporary.cleanup()
                self._jobs.pop(key, None)
        for job in self._jobs.values():
            if (job.scope == scope and job.visit_id == visit_id and job.fingerprint == fingerprint
                    and job.state == "pending"):
                job.touched = time.monotonic()
                return self._summary(job)
        if sum(job.state == "pending" for job in self._jobs.values()) >= MAX_EXPORT_PENDING:
            raise ValueError("已有卡片正在生成，请稍候再试")
        if len(self._jobs) >= MAX_EXPORT_JOBS:
            raise ValueError("临时卡片已满，请稍候重试；生成结果会在 10 分钟后清理")
        with self.plugin.engine.config_scope(scope):
            timezone = self.plugin.engine.world.timezone
        job_id = uuid4().hex
        job = _Export(job_id, scope, visit_id, fingerprint,
                      tempfile.TemporaryDirectory(prefix="my_world_card_export_"), time.monotonic())
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._render(job, snapshot, timezone), name="world-card-export:" + job_id)
        if self._janitor is None:
            self._janitor = asyncio.create_task(self._cleanup_loop(), name="world-card-export-cleanup")
        return self._summary(job)

    async def _render(self, job: _Export, snapshot: dict, timezone: str) -> None:
        renderer = VisitCardRenderer(self.plugin)
        refs: list[str] = []
        try:
            async with asyncio.timeout(EXPORT_TIMEOUT):
                async with self._gate:
                    refs = await renderer.render(job.scope, snapshot, timezone=timezone)
                    if not refs:
                        raise ValueError("这份记录暂时没有可以生成的内容")
                    pages, total = [], 0
                    for index, ref in enumerate(refs):
                        data = await _image_bytes(ref)
                        total += len(data)
                        if total > MAX_EXPORT_BYTES:
                            raise ValueError("这份记录生成的图片过大，暂时无法打包")
                        extension, mime, width, height = await _disk(_image_info, data)
                        filename = f"my-world-visit-{job.id[:12]}-{index + 1:02}.{extension}"
                        path = Path(job.temporary.name) / filename
                        await _disk(path.write_bytes, data)
                        pages.append({"index": index, "filename": filename, "mime": mime,
                                      "width": width, "height": height, "path": path})
                    def bundle():
                        target = Path(job.temporary.name) / f"my-world-visit-{job.id[:12]}.zip"
                        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                            for page in pages:
                                archive.write(page["path"], arcname=page["filename"])
                    await _disk(bundle)
                    job.pages, job.state = pages, "ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.state = "failed"
            job.error = "卡片暂时生成失败，请重新生成；原始相见记录仍可查看"
            self.plugin.logger.warning(f"[my_world] WebUI 相见卡片生成失败：{type(exc).__name__}")
        finally:
            renderer.release(refs)
            job.touched = time.monotonic()

    async def _get(self, scope: str, visit_id: str, job_id: str) -> _Export:
        self._prune()
        job = self._jobs.get(job_id)
        if self._closed or not job or job.scope != scope or job.visit_id != visit_id:
            raise ValueError("生成结果已过期或不属于当前记录，请重新生成")
        snapshot = await self.plugin.visits.record_card_snapshot(scope, visit_id)
        if self._closed or self._jobs.get(job_id) is not job:
            raise ValueError("生成结果已过期，请重新生成")
        if digest(snapshot) != job.fingerprint:
            raise ValueError("相见记录已有更新，请重新生成卡片")
        job.touched = time.monotonic()
        return job

    async def status(self, scope: str, visit_id: str, job_id: str) -> dict:
        return self._summary(await self._get(scope, visit_id, job_id))

    async def page(self, scope: str, visit_id: str, job_id: str, index: int) -> dict:
        job = await self._get(scope, visit_id, job_id)
        if job.state != "ready" or type(index) is not int or not 0 <= index < len(job.pages):
            raise ValueError("请等待生成完成，并选择有效卡片页码")
        page = job.pages[index]
        data = await _disk(page["path"].read_bytes)
        return {"index": index, "filename": page["filename"], "mime": page["mime"],
                "data_url": "data:" + page["mime"] + ";base64," + base64.b64encode(data).decode("ascii")}

    async def download(self, scope: str, visit_id: str, job_id: str, index: int | None) -> tuple[Path, str, str]:
        job = await self._get(scope, visit_id, job_id)
        if job.state != "ready":
            raise ValueError("请等待卡片生成完成后再下载")
        if index is None:
            filename = self._summary(job)["filename"]
            return Path(job.temporary.name) / filename, filename, "application/zip"
        if type(index) is not int or not 0 <= index < len(job.pages):
            raise ValueError("请选择有效卡片页码")
        page = job.pages[index]
        return page["path"], page["filename"], page["mime"]

    async def close(self) -> None:
        self._closed = True
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        if self._janitor is not None:
            tasks.append(self._janitor)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            job.temporary.cleanup()
        self._jobs.clear()
