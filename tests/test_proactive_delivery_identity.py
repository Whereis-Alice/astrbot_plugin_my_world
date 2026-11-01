"""Hook edits must retain each surviving line's recipient and receipt identity."""
import copy
import importlib
import sys
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_group_delivery as group
from tests.test_group_delivery import At, Plain


class HookEvent:
    def __init__(self, target):
        self.unified_msg_origin = target
        self.message_obj = SimpleNamespace(sender=SimpleNamespace(user_id="333", nickname="群友"),
                                           message_id="last", message=[], raw_message=None)
        self.message_str = "群里最后一句话"
        self.result = None

    def get_extra(self, *_):
        return None

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_group_id(self):
        return self.unified_msg_origin.split(":", 2)[2]

    def set_result(self, result):
        self.result = result

    def get_result(self):
        return self.result


class HookResult:
    def __init__(self, chain):
        self.chain = chain

    def set_result_content_type(self, *_):
        pass


class ProactiveDeliveryIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = group.GroupDeliveryTests.asyncSetUp

    @contextmanager
    def hook_environment(self, hook):
        original = HookEvent(self.session)
        self.plugin.last_event = lambda _: original
        self.plugin.context.tool_loop_agent = AsyncMock()
        components = types.ModuleType("astrbot.core.message.components")
        components.Plain = Plain
        results = types.ModuleType("astrbot.core.message.message_event_result")
        results.MessageEventResult = HookResult
        results.ResultContentType = SimpleNamespace(LLM_RESULT="llm")

        def response(text, raw):
            return SimpleNamespace(completion_text=text, result_chain=SimpleNamespace(chain=[Plain(text)]))

        with patch.dict(sys.modules, {components.__name__: components, results.__name__: results}), \
                patch.object(self.delivery, "isolated_event", side_effect=lambda event, *_: copy.deepcopy(event)), \
                patch.object(self.delivery, "public_response", side_effect=response), \
                patch.object(self.delivery, "fire_hook", side_effect=hook), \
                patch.object(self.delivery, "accepted", AsyncMock()):
            yield original

    def two_contacts(self):
        world = self.plugin.store.raw_world()
        world["proactive_session_whitelist"] = [self.session]
        self.plugin.store.save_world(world)
        self.plugin.engine.reload_config()
        model = importlib.import_module(self.module.__name__ + ".core.engine.models")
        view = model.TickOutcome(self.session, speech_kind="outreach")
        for uid, name in (("111", "第一位"), ("222", "第二位")):
            self.plugin.engine.profiles.touch(self.session, uid, name)
            view.contact_people = {uid}
            text = name + "，最近怎么样？"
            view.speech_decisions.append({"id": uid, "target": self.session, "drafts": [text],
                                          "status": "prepared", "at": self.plugin.engine._now(),
                                          "intent": "outreach", "origin": "outreach", "sent": []})
            view.add_speech(text)
        return view

    async def test_each_public_hook_observes_its_lines_recipient(self):
        view = self.two_contacts()
        observed = []

        async def hook(_plugin, event, name, *args):
            if name == "OnLLMResponseEvent":
                observed.append(event.get_sender_id())
            return False

        with self.hook_environment(hook) as original:
            async with self.delivery.delivery(self.plugin, self.session, view):
                pass
        self.assertEqual(observed, ["111", "222"])
        self.assertEqual(original.get_sender_id(), "333")

    async def check_suppressed_line(self, phase):
        view = self.two_contacts()
        calls = 0

        async def hook(_plugin, event, name, *args):
            nonlocal calls
            if name == "OnLLMResponseEvent":
                calls += 1
                if calls == 1 and phase == "response":
                    return True
            if name == "OnDecoratingResultEvent":
                if calls == 1 and phase == "decoration":
                    return True
                if calls == 1 and phase == "empty_body":
                    event.get_result().chain = [At("111")]
            return False

        with self.hook_environment(hook):
            await self.plugin.engine._deliver(view)
        self.plugin.context.send_message.assert_awaited_once()
        chain = self.plugin.context.send_message.await_args.args[1].chain
        body = "".join(part.text for part in chain if isinstance(part, Plain))
        self.assertIn("第二位，最近怎么样？", body)
        self.assertIn("@第二位", body)
        self.assertNotIn("第一位", body)
        self.assertEqual(self.plugin.engine.contact_status(self.session, "111")["person_left"], 2)
        self.assertEqual(self.plugin.engine.contact_status(self.session, "222")["person_left"], 1)
        self.assertFalse(view.delivered_decisions.get("111"))
        self.assertEqual(view.delivered_decisions["222"], ["第二位，最近怎么样？"])

    async def test_response_suppression_keeps_actual_recipient_and_quota(self):
        await self.check_suppressed_line("response")

    async def test_decoration_suppression_keeps_actual_recipient_and_quota(self):
        await self.check_suppressed_line("decoration")

    async def test_metadata_only_replacement_keeps_actual_recipient_and_quota(self):
        await self.check_suppressed_line("empty_body")

    async def test_legacy_line_without_recipient_metadata_keeps_explicit_contact(self):
        view = self.two_contacts()
        view.messages = [view.messages[0]]
        view.contact_people = {"111"}
        view.speech_people.clear()
        observed = []

        async def hook(_plugin, event, name, *args):
            if name == "OnLLMResponseEvent":
                observed.append(event.get_sender_id())
            return False

        with self.hook_environment(hook):
            await self.plugin.engine._deliver(view)
        self.assertEqual(observed, ["111"])
        body = "".join(part.text for part in self.plugin.context.send_message.await_args.args[1].chain
                       if isinstance(part, Plain))
        self.assertIn("@第一位", body)

    async def test_instant_filter_keeps_surviving_origin_and_debug_position(self):
        view = self.two_contacts()
        view.speech_origins[""] = ["original-first", "original-second"]
        view.debug_messages, view.debug_positions = ["动作说明"], [1]
        calls = 0

        async def hook(_plugin, event, name, *args):
            nonlocal calls
            if name == "OnLLMResponseEvent":
                calls += 1
                return calls == 1
            return False

        with self.hook_environment(hook):
            async with self.delivery.delivery(self.plugin, self.session, view, instant=True):
                self.assertEqual(view.speech_origins[""], ["original-second"])
                self.assertEqual(view.speech_ids[""], ["222"])
                self.assertEqual(view.debug_positions, [0])
                self.assertIsNotNone(self.delivery.prepared_chain(self.session, view.messages[0], 0))
