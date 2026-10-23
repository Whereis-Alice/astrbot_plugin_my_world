"""Malformed historical fields must not break log browsing or export."""
import copy
import unittest

from core.timeline import build_timeline, render_event
from tests import test_record_sessions_api as records


class TimelineResilienceTests(unittest.TestCase):
    def test_invalid_image_count_keeps_the_original_record_and_other_events(self):
        rows = [{"id": 1, "event_type": "reply", "detail": {
                    "messages": ["刚给花浇了水"], "images": "两张", "session": "私聊 100"}},
                {"id": 2, "event_type": "bot_spoke", "detail": {}}]
        original = copy.deepcopy(rows)
        result = build_timeline(rows)
        self.assertEqual(rows, original)
        self.assertEqual([row["id"] for row in result], [1, 2])
        self.assertIn("刚给花浇了水", result[0]["text"])
        self.assertIn("images=两张", result[0]["text"])
        self.assertEqual(result[0]["text"].count("私聊 100"), 1)
        self.assertEqual(result[1]["text"], "她说了话，等待回应")

    def test_invalid_nested_query_retains_its_raw_content(self):
        text = render_event({"event_type": "recall_start", "detail": {
            "intent": "记起上周的约定", "query": ["旧格式的地点"]}})
        self.assertIn("记起上周的约定", text)
        self.assertIn("旧格式的地点", text)

    def test_unhashable_intent_and_nonfinite_count_are_isolated(self):
        rows = [{"event_type": "speech_result", "detail": {
                    "intent": {"old": "分享生活"}, "status": "sent"}},
                {"event_type": "vision", "detail": {"images": float("inf"), "detail": "窗边的花"}}]
        result = build_timeline(rows)
        self.assertIn("分享生活", result[0]["text"])
        self.assertIn("窗边的花", result[1]["text"])

    def test_extension_output_still_takes_precedence(self):
        event = {"event_type": "reply", "detail": {"images": "两张", "session": "测试群"}}
        text = render_event(event, ext_render=lambda kind, detail: "扩展的说明")
        self.assertEqual(text, "扩展的说明　〔在 测试群〕")

    def test_failed_extension_keeps_the_builtin_rendering(self):
        def failing_extension(kind, detail):
            raise RuntimeError("扩展不可用")
        event = {"event_type": "reply", "detail": {"images": "两张"}}
        self.assertIn("images=两张", render_event(event, ext_render=failing_extension))


class PersistedLogResilienceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = records.RecordSessionsApiTests.asyncSetUp
    call = records.RecordSessionsApiTests.call

    async def seed_logs(self):
        good = await self.plugin.db.call("add_event", session_id=self.session,
                                        event_type="bot_spoke", detail={})
        bad = await self.plugin.db.call("add_event", session_id=self.session,
            event_type="reply", detail={"images": "两张", "messages": ["今天读完了一章"]})
        await self.plugin.db.call("add_event", session_id="test:GroupMessage:200",
                                 event_type="reply", detail={"messages": ["另一个会话的内容"]})
        return good, bad

    async def test_log_list_survives_invalid_persisted_fields(self):
        ids = await self.seed_logs()
        response = await self.call("logs", session=self.session)
        self.assertEqual(response["status_code"], 200)
        rows = response["data"]["events"]
        self.assertEqual({row["id"] for row in rows}, set(ids))
        bad = next(row for row in rows if row["id"] == ids[1])
        self.assertEqual(bad["detail"]["images"], "两张")
        self.assertIn("今天读完了一章", bad["text"])
        stored = await self.plugin.db.call("query_events", session_id=self.session)
        self.assertEqual(next(row for row in stored if row["id"] == ids[1])["detail"], bad["detail"])

    async def test_log_export_keeps_both_valid_and_invalid_records(self):
        ids = await self.seed_logs()
        response = await self.call("logs/export", session=self.session)
        self.assertEqual(response["status_code"], 200)
        rows = response["data"]["events"]
        self.assertEqual({row["id"] for row in rows}, set(ids))
        self.assertTrue(all(row["session_id"] == self.session for row in rows))
        self.assertIn("images=两张", next(row for row in rows if row["id"] == ids[1])["text"])
