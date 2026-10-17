"""LLM token 用量收集器的单元测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.llm_usage import collect_llm_usage, record_llm_usage


class LLMUsageTests(unittest.TestCase):
    def test_collects_standard_token_usage(self):
        with collect_llm_usage() as usage:
            record_llm_usage(
                SimpleNamespace(input_other=120, input_cached=30, output=45)
            )
            record_llm_usage(
                SimpleNamespace(input_other=80, input_cached=0, output=20)
            )
        payload = usage.payload()
        self.assertTrue(payload["available"])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["responses"], 2)
        self.assertEqual(payload["input"], 230)
        self.assertEqual(payload["input_cached"], 30)
        self.assertEqual(payload["output"], 65)
        self.assertEqual(payload["total"], 295)

    def test_missing_provider_usage_is_reported_as_partial(self):
        with collect_llm_usage() as usage:
            record_llm_usage(
                SimpleNamespace(input_other=10, input_cached=0, output=5)
            )
            record_llm_usage(None)
        payload = usage.payload()
        self.assertTrue(payload["available"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["reported_responses"], 1)
        self.assertEqual(payload["responses"], 2)

    def test_openai_style_usage_keys_are_supported(self):
        with collect_llm_usage() as usage:
            record_llm_usage({"prompt_tokens": 90, "completion_tokens": 30})
        payload = usage.payload()
        self.assertEqual(payload["input"], 90)
        self.assertEqual(payload["output"], 30)
        self.assertEqual(payload["total"], 120)

    def test_record_outside_a_generation_task_is_a_noop(self):
        record_llm_usage(SimpleNamespace(input_other=999, output=999))


if __name__ == "__main__":
    unittest.main()
