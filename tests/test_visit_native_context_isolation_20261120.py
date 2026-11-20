"""Federation decisions stay structured when an outward task is in context."""
from __future__ import annotations

import importlib
import unittest
from unittest.mock import AsyncMock, patch

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_integration_integrity as fixture  # noqa: E402


class VisitNativeContextIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.IntegrationIntegrityTests.asyncSetUp
    response = fixture.IntegrationIntegrityTests.response

    async def test_inherited_speech_cannot_rebuild_visit_as_native_chat(self):
        runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        model_calls = importlib.import_module(self.module.__name__ + ".core.model_calls")
        native = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock(return_value=self.response('{"say":"晚安。"}'))
        for role in ("main", "helper"):
            adapter = self.adapter_module.AstrBotLLM(self.plugin, provider_id="provider-a", role=role)
            token = runtime.SPEECH_REQUEST.set({"target": self.session, "rows": [],
                                               "allow_tools": True, "channel": "helper"})
            try:
                with patch.object(native.ProactiveContext, "prepare", AsyncMock()) as prepare:
                    with model_calls.model_context("world_visit", "友邻串门：departure", self.session):
                        result = await adapter.generate(session_id=self.session, prompt="只说自己的告别",
                                                        system_prompt="只输出say", max_tokens=1024)
                    self.assertTrue(result.ok)
                    self.assertEqual(result.text, '{"say":"晚安。"}')
                    prepare.assert_not_awaited()
            finally:
                runtime.SPEECH_REQUEST.reset(token)
        self.assertEqual(self.plugin.context.llm_generate.await_count, 2)
        self.plugin.context.tool_loop_agent.assert_not_awaited()
        for request in self.plugin.context.llm_generate.call_args_list:
            self.assertEqual(request.kwargs["prompt"], "只说自己的告别")
            self.assertEqual(request.kwargs["max_tokens"], 1024)


if __name__ == "__main__":
    unittest.main()
