"""Model-call attribution, metering, persistence and native-hook boundaries."""

import asyncio
import importlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.db import Database
from core.llm_usage import collect_llm_usage, normalized_usage
from core.model_call_runtime import safe_error, tracked_generate
from core.model_calls import call_context, model_context, model_operation
from tests import test_scope_api as scope_api


def response(usage=None):
    return SimpleNamespace(usage=usage, raw_completion={"model": "test-model"}, id="response-1", role="assistant")


def row(**overrides):
    return {"started_at": time.time(), "duration_ms": 120, "session_id": "test:GroupMessage:100",
            "provider_id": "test-provider", "model": "test-model", "purpose": "plan", "reason": "生活时钟推进",
            "status": "success", "coverage": "direct", "usage": normalized_usage({"input_other": 80, "input_cached": 20, "output": 30}), **overrides}


class UsageNormalizationTests(unittest.TestCase):
    def test_errors_redact_authorization_without_leaving_the_bearer_value(self):
        for text in ('Authorization: Bearer private-credential', '"Authorization": "Bearer private-credential"',
                     'authorization=Basic private-credential', 'api_key=private-credential', 'Bearer private-credential'):
            self.assertNotIn("private-credential", safe_error(text))

    def test_cache_is_part_of_openai_input_not_an_extra_charge(self):
        for value in ({"prompt_tokens": 100, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 20}},
                      SimpleNamespace(input_tokens=100, output_tokens=30, input_tokens_details=SimpleNamespace(cached_tokens=20))):
            usage = normalized_usage(value)
            self.assertEqual((usage["input_other"], usage["input_cached"], usage["input"], usage["total"]), (80, 20, 100, 130))
            self.assertFalse(usage["partial"])

    def test_missing_partial_zero_and_invalid_values_remain_distinct(self):
        self.assertFalse(normalized_usage(None)["available"])
        self.assertEqual(normalized_usage({"total_tokens": 25})["input"], None)
        self.assertTrue(normalized_usage({"total_tokens": 25})["partial"])
        self.assertEqual(normalized_usage({"input_tokens": 0, "output_tokens": 0})["total"], 0)
        self.assertFalse(normalized_usage({"total_tokens": float("inf"), "input": True, "output": "bad"})["available"])
        with collect_llm_usage() as usage:
            from core.llm_usage import record_llm_usage
            record_llm_usage({"total_tokens": 25})
            record_llm_usage(None)
        self.assertEqual(usage.payload()["total"], 25)
        self.assertTrue(usage.payload()["partial"])


class ModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = SimpleNamespace(db=SimpleNamespace(call=AsyncMock()), logger=Mock(),
                                      context=SimpleNamespace(llm_generate=AsyncMock()))

    async def test_response_unchanged_no_prompt_stored_and_aggregate_once(self):
        result = response({"prompt_tokens": 100, "completion_tokens": 30})
        self.plugin.context.llm_generate.return_value = result
        with collect_llm_usage() as aggregate, model_context("plan", "时钟推进", "group:1"):
            returned = await tracked_generate(self.plugin, chat_provider_id="test-provider", prompt="PRIVATE PROMPT", system_prompt="PRIVATE SYSTEM")
        self.assertIs(returned, result)
        saved = self.plugin.db.call.call_args.args[1]
        self.assertEqual((saved["purpose"], saved["session_id"], saved["chain"], saved["model"]), ("plan", "group:1", ["时钟推进"], "test-model"))
        self.assertNotIn("PRIVATE", str(saved))
        self.assertEqual(aggregate.payload()["responses"], 1)
        self.assertEqual(aggregate.payload()["total"], 130)

    async def test_failures_cancellation_and_storage_failure_do_not_change_control_flow(self):
        self.plugin.context.llm_generate.side_effect = ValueError("token=secret-value https://provider.invalid/path abk_123456789secret")
        with self.assertRaises(ValueError):
            await tracked_generate(self.plugin)
        saved = self.plugin.db.call.call_args.args[1]
        self.assertEqual(saved["status"], "error")
        self.assertIsNone(saved["usage"]["total"])
        self.assertNotIn("secret", saved["error"])
        self.assertNotIn("provider.invalid", saved["error"])
        self.plugin.context.llm_generate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await tracked_generate(self.plugin)
        self.assertEqual(self.plugin.db.call.call_args.args[1]["status"], "cancelled")
        self.plugin.context.llm_generate.side_effect = None
        result = response()
        self.plugin.context.llm_generate.return_value = result
        self.plugin.db.call.side_effect = RuntimeError("storage unavailable")
        self.assertIs(await tracked_generate(self.plugin), result)

    async def test_context_is_nested_and_isolated_between_concurrent_sessions(self):
        class Work:
            @model_operation("plan", "选择活动")
            async def run(self, session_id):
                await asyncio.sleep(0)
                return call_context()
        async def branch(sid):
            with model_context("tick", "生活时钟推进", sid) as parent:
                nested = await Work().run(sid)
                self.assertEqual(call_context()["purpose"], "tick")
                self.assertEqual(nested["trace_id"], parent["trace_id"])
                return nested
        first, second = await asyncio.gather(branch("group:1"), branch("group:2"))
        self.assertEqual(first["chain"], ["生活时钟推进", "选择活动"])
        self.assertEqual((first["session_id"], second["session_id"]), ("group:1", "group:2"))
        self.assertNotEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(call_context(), {})


class ModelCallStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "world.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def test_persistence_filtering_pagination_and_totals_cover_all_results(self):
        for index in range(5):
            self.db.append_model_call(row(reason=f"记录 {index}"))
        self.db.append_model_call(row(session_id="another", status="error", purpose="chat", coverage="native_final", usage=normalized_usage(None)))
        self.db.append_model_call(row(reason="100%_literal"))
        result = self.db.model_calls(session="test:GroupMessage:100", size=2, page=2)
        self.assertEqual((len(result["items"]), result["summary"]["calls"], result["summary"]["total"], result["pages"]), (2, 6, 780, 3))
        self.assertEqual(self.db.model_calls(q="%_")["summary"]["calls"], 1)
        self.assertEqual(self.db.model_calls(q="' OR 1=1 --")["summary"]["calls"], 0)
        self.assertEqual(self.db.model_calls(status="error")["summary"]["final_only"], 1)
        self.assertEqual(self.db.model_calls(status="error")["summary"]["reported"], 0)
        self.assertEqual(sum(item["tokens"] for item in result["series"]), 780)
        self.db.close()
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.model_calls()["summary"]["calls"], 7)

    def test_retention_is_bounded_by_time_and_count_and_paging_is_clamped(self):
        self.db.append_model_call(row(started_at=time.time() - 31 * 86400))
        self.assertEqual(self.db.model_calls(hours=720)["summary"]["calls"], 0)
        # Seed a long-running installation without making 10,000 separate transactions.
        with self.db.atomic():
            self.db._execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10001) INSERT INTO model_call(started_at,metadata) SELECT ?, '{}' FROM n", (time.time(),))
        self.db.append_model_call(row())
        self.assertEqual(self.db._query("SELECT COUNT(*) FROM model_call")[0][0], 10000)
        result = self.db.model_calls(page=-10, size=500, hours=999)
        self.assertEqual((result["page"], len(result["items"]), result["hours"]), (1, 100, 24))


class ModelCallsApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope_api.ScopeApiTests.asyncSetUp
    call = scope_api.ScopeApiTests.call

    async def test_api_uses_explicit_filters_independently_of_edit_scope_and_is_guarded(self):
        await self.plugin.db.call("append_model_call", row(session_id=self.session))
        await self.plugin.db.call("append_model_call", row(session_id="other"))
        self.plugin.engine.note_session_name(self.session, "日常聊天")
        result = await self.call("model-calls", config_scope=self.session, session=self.session)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["summary"]["calls"], 1)
        self.assertIn("日常聊天", result["data"]["items"][0]["session_label"])
        result = await self.call("model-calls", config_scope=self.session)
        self.assertEqual(result["data"]["summary"]["calls"], 2)
        self.plugin._guard = lambda payload=None: {"status_code": 401}
        self.assertEqual((await self.call("model-calls"))["status_code"], 401)

    async def test_adapter_retries_share_trace_and_record_each_attempt_once(self):
        adapter_module = importlib.import_module(self.module.__name__ + ".adapters.llm")
        context_module = importlib.import_module(self.module.__name__ + ".core.model_calls")
        result = response({"input_other": 10, "output": 5})
        result.completion_text = "好了"
        self.plugin.context.llm_generate = AsyncMock(side_effect=[TypeError("sampling unsupported"), result])
        llm = adapter_module.AstrBotLLM(self.plugin, provider_id="test", role="helper", sampling={"temperature": .8, "top_p": .9})
        with context_module.model_context("helper", "补全工具参数", self.session):
            reply = await llm.generate(session_id=self.session, system_prompt="", prompt="x")
        self.assertTrue(reply.ok)
        rows = (await self.plugin.db.call("model_calls"))["items"]
        self.assertEqual([item["status"] for item in rows], ["success", "error"])
        self.assertEqual([item["attempt"] for item in rows], [2, 1])
        self.assertEqual(rows[0]["trace_id"], rows[1]["trace_id"])
        self.assertIn("采样参数", rows[0]["retry_reason"])
        self.assertEqual(rows[0]["purpose"], "helper")

    async def test_native_final_is_once_only_ignores_chunks_and_observed_plugin_calls(self):
        from tests.test_dialogue_inject import Event
        event = Event("hello", session=self.session)
        event.set_extra(self.module.MW_TURN_EXTRA, {"started_at": time.time() - .1,
            "model_attribution": {"trace_id": "native-turn-trace", "chain": ["收到用户消息，进入回复流程"]}})
        self.plugin.profile_weaver_service = None
        final = response({"input_other": 12, "output": 3})
        final.completion_text = "hello"
        chunk = response()
        chunk.is_chunk = True
        await self.plugin.on_llm_response(event, chunk)
        self.assertEqual((await self.plugin.db.call("model_calls"))["summary"]["calls"], 0)
        token = self.plugin.set_self_initiated()
        try:
            await self.plugin.on_llm_response(event, final)
        finally:
            self.plugin.reset_self_initiated(token)
        event._profileweaver_auto_extract_running = True
        await self.plugin.on_llm_response(event, final)
        event._profileweaver_auto_extract_running = False
        await self.plugin.on_llm_response(event, final)
        await self.plugin.on_llm_response(event, final)
        result = await self.plugin.db.call("model_calls")
        self.assertEqual(result["summary"]["calls"], 1)
        self.assertEqual(result["summary"]["total"], 15)
        self.assertEqual(result["items"][0]["coverage"], "native_final")
        self.assertEqual(result["items"][0]["trace_id"], "native-turn-trace")
        self.assertEqual(result["items"][0]["chain"], ["收到用户消息，进入回复流程"])
