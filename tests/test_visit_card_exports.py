"""Manual exports use full server records and never authorize message delivery."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import copy
import importlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from tests import test_visit_card_adapter

exports = importlib.import_module("visit_adapter_fixture.web.visit_card_exports")


class VisitCardExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from PIL import Image
        self.temporary = tempfile.TemporaryDirectory(prefix="test_card_export_source_")
        self.source = Path(self.temporary.name) / "a %20 fixture.png"
        image = Image.new("RGB", (1350, 120), "#d9cff6")
        image.save(self.source)
        self.snapshot = {"scope": "life-a", "id": "met", "status": "completed", "arrived_at": 100,
                         "local": {"name": "艾拉"}, "remote": {"name": "爱丽丝"}, "log": []}
        self.plugin = SimpleNamespace(retired=False,
            visits=SimpleNamespace(record_card_snapshot=AsyncMock(side_effect=lambda *_: copy.deepcopy(self.snapshot))),
            engine=SimpleNamespace(config_scope=lambda _: nullcontext(), world=SimpleNamespace(timezone="Asia/Shanghai")),
            logger=SimpleNamespace(warning=Mock()), context=SimpleNamespace(send_message=AsyncMock()))
        self.renderer = SimpleNamespace(render=AsyncMock(return_value=[self.source.as_uri(), self.source.as_uri()]),
                                        release=Mock())
        self.patcher = patch.object(exports, "VisitCardRenderer", return_value=self.renderer)
        self.patcher.start()
        self.manager = exports.VisitCardExports(self.plugin)

    async def asyncTearDown(self):
        await self.manager.close()
        self.patcher.stop()
        self.temporary.cleanup()
        self.plugin.context.send_message.assert_not_awaited()

    async def ready(self):
        started = await self.manager.start("life-a", "met")
        job = self.manager._jobs[started["job_id"]]
        await asyncio.wait_for(job.task, 4)
        return job, await self.manager.status("life-a", "met", job.id)

    async def test_ready_preview_and_original_downloads_are_independent_and_complete(self):
        first = await self.manager.start("life-a", "met")
        second = await self.manager.start("life-a", "met")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["state"], "pending")
        job = self.manager._jobs[first["job_id"]]
        await asyncio.wait_for(job.task, 4)
        ready = await self.manager.status("life-a", "met", job.id)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(len(ready["pages"]), 2)
        self.assertNotIn("path", str(ready))
        self.assertEqual(ready["pages"][0]["width"], 1350)
        self.renderer.render.assert_awaited_once()
        preview = await self.manager.page("life-a", "met", job.id, 1)
        self.assertTrue(preview["data_url"].startswith("data:image/png;base64,"))
        image, name, mime = await self.manager.download("life-a", "met", job.id, 1)
        self.assertEqual((image.name, mime), (name, "image/png"))
        self.assertEqual(image.read_bytes(), self.source.read_bytes())
        bundle, name, mime = await self.manager.download("life-a", "met", job.id, None)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(archive.namelist(), [page["filename"] for page in ready["pages"]])
            self.assertTrue(all(archive.read(item) == self.source.read_bytes() for item in archive.namelist()))
        self.assertEqual(mime, "application/zip")
        self.renderer.release.assert_called_once()

    async def test_wrong_life_record_job_and_page_are_rejected(self):
        job, _ = await self.ready()
        for scope, visit, job_id in (("life-b", "met", job.id), ("life-a", "else", job.id),
                                      ("life-a", "met", "../../unknown")):
            with self.subTest(scope=scope, visit=visit, job=job_id), self.assertRaises(ValueError):
                await self.manager.status(scope, visit, job_id)
        for index in (True, -1, 2, 0.5, "0"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                await self.manager.download("life-a", "met", job.id, index)

    async def test_regenerate_after_completion_really_renders_again(self):
        first, _ = await self.ready()
        second, result = await self.ready()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.renderer.render.await_count, 2)

    async def test_queue_wait_is_included_in_job_deadline(self):
        async def slow(*_, **__):
            await asyncio.Future()
        self.renderer.render.side_effect = slow
        with patch.object(exports, "EXPORT_TIMEOUT", .03):
            first = await self.manager.start("life-a", "met")
            second = await self.manager.start("life-a", "met-two")
            jobs = [self.manager._jobs[item["job_id"]] for item in (first, second)]
            await asyncio.wait_for(asyncio.gather(*(job.task for job in jobs)), .5)
            self.assertTrue(all(job.state == "failed" for job in jobs))

    async def test_changed_record_requires_new_generation(self):
        job, _ = await self.ready()
        self.snapshot["log"].append({"kind": "say", "text": "后来确认的话"})
        with self.assertRaisesRegex(ValueError, "已有更新"):
            await self.manager.page("life-a", "met", job.id, 0)
        again = await self.manager.start("life-a", "met")
        self.assertNotEqual(again["job_id"], job.id)

    async def test_failure_is_sanitized_and_retry_replaces_failed_job(self):
        self.renderer.render.side_effect = RuntimeError("private endpoint credential")
        job, result = await self.ready()
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("credential", str(result))
        directory = Path(job.temporary.name)
        self.renderer.render.side_effect = None
        started = await self.manager.start("life-a", "met")
        self.assertNotEqual(started["job_id"], job.id)
        self.assertFalse(directory.exists())

    async def test_expiry_and_close_remove_only_export_files(self):
        job, _ = await self.ready()
        directory = Path(job.temporary.name)
        job.touched -= exports.EXPORT_TTL + 1
        with self.assertRaisesRegex(ValueError, "过期"):
            await self.manager.status("life-a", "met", job.id)
        self.assertFalse(directory.exists())
        self.assertTrue(self.source.exists())
        second, _ = await self.ready()
        directory = Path(second.temporary.name)
        await self.manager.close()
        self.assertFalse(directory.exists())
        with self.assertRaises(ValueError):
            await self.manager.start("life-a", "met")

    async def test_render_queue_is_bounded_and_close_joins_pending_work(self):
        entered = asyncio.Event()
        async def slow(*_, **__):
            entered.set()
            await asyncio.Future()
        self.renderer.render.side_effect = slow
        first = await self.manager.start("life-a", "met")
        await entered.wait()
        self.snapshot["id"] = "met-two"
        await self.manager.start("life-a", "met-two")
        with self.assertRaisesRegex(ValueError, "正在生成"):
            await self.manager.start("life-a", "met-three")
        job = self.manager._jobs[first["job_id"]]
        directory = Path(job.temporary.name)
        await self.manager.close()
        self.assertTrue(job.task.done())
        self.assertFalse(directory.exists())

    async def test_invalid_renderer_output_never_becomes_download(self):
        self.renderer.render.return_value = ["base64://bm90LWFuLWltYWdl"]
        job, result = await self.ready()
        self.assertEqual(result["state"], "failed")
        with self.assertRaises(ValueError):
            await self.manager.download("life-a", "met", job.id, None)


class VisitCardExportApiTests(unittest.IsolatedAsyncioTestCase):
    from tests import test_visits_web as fixture
    setUp = fixture.VisitEditorApiTests.setUp
    call = fixture.VisitEditorApiTests.call

    def setup_exports(self):
        self.plugin.visit_card_exports = SimpleNamespace(**{
            name: AsyncMock(return_value={"state": "pending", "job_id": "job"})
            for name in ("start", "status", "page", "download")})
        return self.plugin.visit_card_exports

    async def test_exports_require_auth_and_resolve_original_scope(self):
        service = self.setup_exports()
        for endpoint in ("visits/card-export", "visits/card-export-status", "visits/card-export-page", "visits/card-export-download"):
            result = await self.call(endpoint, authenticated=False, visit_id="met", job_id="job", page=0)
            self.assertEqual(result["status_code"], 401)
        self.assertTrue(all(not method.called for method in vars(service).values()))
        result = await self.call("visits/card-export", visit_id="met", log=[{"text": "client fake"}])
        self.assertEqual(result["status_code"], 200)
        service.start.assert_awaited_once_with("shared-life", "met")
        result = await self.call("visits/card-export-status", visit_id="met", job_id="job")
        self.assertEqual(result["status_code"], 200)
        service.status.assert_awaited_once_with("shared-life", "met", "job")

    async def test_download_uses_binary_file_response_and_strict_index(self):
        service = self.setup_exports()
        service.download.return_value = (Path("owned.png"), "record.png", "image/png")
        with patch.object(self.fixture.api, "file_response", return_value="binary file") as response:
            result = await self.call("visits/card-export-download", visit_id="met", job_id="job", page="0")
            self.assertEqual(result, "binary file")
            service.download.assert_awaited_once_with("shared-life", "met", "job", 0)
            self.assertEqual(response.call_args.kwargs["headers"]["Cache-Control"], "no-store")
        for index in (True, "-1", "1.2", "../../x", [], "99999999999999"):
            with self.subTest(index=index):
                result = await self.call("visits/card-export-page", visit_id="met", job_id="job", page=index)
                self.assertEqual(result["status_code"], 400)
        service.page.assert_not_awaited()
