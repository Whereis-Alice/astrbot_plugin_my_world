"""Real registered API handlers: editor guards, manual overrides and conversation boundaries."""

import unittest
from tests import test_life_api as life


class SocialApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = life.LifeApiTests.asyncSetUp
    call = life.LifeApiTests.call

    async def test_vocabulary_stays_in_selected_conversation_and_disable_survives(self):
        result = await self.call(
            "social-learning/update",
            method="POST",
            session=self.session,
            operation="vocabulary",
            term="夜航",
            description="晚上一起读书",
        )
        self.assertEqual(result["status_code"], 200)
        data = (await self.call("social-learning", session=self.session))["data"]
        self.assertEqual(data["vocabulary"][0]["term"], "夜航")
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        self.assertEqual(
            (await self.call("social-learning", session=other))["data"]["vocabulary"], []
        )
        await self.call(
            "social-learning/update",
            method="POST",
            session=self.session,
            operation="vocabulary",
            term="夜航",
            description="晚上一起读书",
            status="disabled",
        )
        self.assertFalse(
            self.plugin.db.raw.set_vocabulary(
                self.session, "夜航", "不同含义", evidence=[{"text": "不能覆盖"}]
            )
        )

    async def test_person_override_and_stale_request_cannot_change_newer_data(self):
        self.plugin.engine.profiles.touch(self.session, "42", "小明")
        bonds_before = self.plugin.engine.profiles.bonds(self.session, "42")
        params = {
            "session": self.session,
            "user_id": "42",
            "operation": "habits",
            "revision": 0,
            "text": "先听他说完",
        }
        self.assertEqual(
            (await self.call("social-learning/update", method="POST", **params))["status_code"], 200
        )
        params["text"] = "过期内容"
        self.assertEqual(
            (await self.call("social-learning/update", method="POST", **params))["status_code"], 400
        )
        detail = (await self.call("social-learning", session=self.session, user_id="42"))["data"][
            "interaction"
        ]
        self.assertTrue(detail["manual"])
        self.assertEqual(detail["habits"][0]["text"], "先听他说完")
        result = await self.call(
            "social-learning/update",
            method="POST",
            session=self.session,
            user_id="42",
            operation="resume",
            revision=1,
        )
        self.assertEqual(result["status_code"], 200)
        self.assertFalse(self.plugin.db.raw.interaction_detail(self.session, "42")["manual"])
        self.assertEqual(self.plugin.engine.profiles.bonds(self.session, "42"), bonds_before)

    async def test_guard_missing_scope_and_unknown_person(self):
        self.assertEqual((await self.call("social-learning"))["status_code"], 400)
        self.assertEqual(
            (await self.call("social-learning", session=self.session, user_id="unknown"))[
                "status_code"
            ],
            400,
        )
        self.plugin._guard = lambda payload=None: {"status_code": 403}
        self.assertEqual(
            (await self.call("social-learning", session=self.session))["status_code"], 403
        )
        self.assertEqual(
            (await self.call("decision-traces", session=self.session))["status_code"], 403
        )
        self.assertEqual(
            (
                await self.call(
                    "social-learning/update",
                    method="POST",
                    session=self.session,
                    operation="vocabulary",
                    term="x",
                    description="x",
                )
            )["status_code"],
            403,
        )

    async def test_decision_trace_separates_silence_error_and_missing_completion(self):
        for state in ("silent", "error", "awaiting_native"):
            self.plugin.db.raw.append_decision_trace(self.session, "trace", "gate", state, state)
        self.plugin.db.raw.append_decision_trace("private", "trace", "send", "sent", "秘密")
        result = (await self.call("decision-traces", session=self.session, trace="trace"))["data"][
            "items"
        ]
        self.assertEqual({row["status"] for row in result}, {"silent", "error", "awaiting_native"})
