"""Group addressing must remain readable when OneBot member names are empty."""
import importlib
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_integration_integrity as integration


class Plain(dict):
    def __init__(self, text):
        super().__init__(type="plain", text=text)
        self.text = text


class At(dict):
    def __init__(self, qq, name=""):
        super().__init__(type="at", qq=qq, name=name)
        self.qq, self.name = qq, name

    def toDict(self):
        # Native AstrBot drops name when serializing to OneBot.
        return {"type": "at", "data": {"qq": str(self.qq)}}


class Reply(dict):
    def __init__(self, id):
        super().__init__(type="reply", id=id)
        self.id = id


class GroupDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await integration.IntegrationIntegrityTests.asyncSetUp(self)
        self.messenger = importlib.import_module(self.module.__name__ + ".adapters.messenger")
        self.delivery = importlib.import_module(self.module.__name__ + ".adapters.proactive_delivery")
        self.policy = importlib.import_module(self.module.__name__ + ".core.speech_policy")
        from astrbot.api import message_components
        for module in (self.messenger, message_components):
            for name, value in (("Plain", Plain), ("At", At), ("Reply", Reply)):
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value={"user_id": 42, "card": "", "nickname": ""}))
        self.platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: self.bot)
        self.plugin.context.get_platform_inst = lambda _: self.platform
        self.plugin.context.send_message = AsyncMock(return_value=True)
        self.plugin.typing_delay_for = lambda _: 0
        self.plugin.engine.profiles.touch(self.session, "42", "阿岚")

    def prepared(self, chain, text="你刚才说的那本书"):
        token = self.delivery.WIRE.set({(self.session, 0): {"text": text, "chain": chain}})
        self.addCleanup(self.delivery.WIRE.reset, token)
        return text

    def sent(self):
        return self.plugin.context.send_message.await_args.args[1].chain

    async def test_empty_protocol_name_uses_known_visible_calling_name(self):
        with self.policy.contact_send(self.session, {"42"}):
            self.assertTrue(await self.plugin.messenger.send_text(self.session, ["最近怎么样"]))
        self.assertFalse(any(isinstance(part, At) for part in self.sent()))
        self.assertIn("@阿岚", "".join(part.text for part in self.sent() if isinstance(part, Plain)))
        self.assertEqual(self.plugin.messenger.take_sent_texts(self.session), ["最近怎么样"])

    async def test_resolved_member_keeps_real_mention_with_display_name(self):
        self.bot.call_action.return_value = {"user_id": 42, "card": "群里的阿岚", "nickname": "阿岚"}
        with self.policy.contact_send(self.session, {"42"}):
            await self.plugin.messenger.send_text(self.session, ["最近怎么样"])
        mention = next(part for part in self.sent() if isinstance(part, At))
        self.assertEqual((str(mention.qq), mention.name), ("42", "群里的阿岚"))
        self.assertEqual(mention.toDict()["data"]["qq"], "42")

    async def test_hook_and_outreach_do_not_duplicate_the_same_mention(self):
        self.bot.call_action.return_value = {"user_id": 42, "nickname": "阿岚"}
        text = self.prepared([At(qq="42", name="阿岚"), Plain("你刚才说的那本书")])
        with self.policy.contact_send(self.session, {"42"}):
            await self.plugin.messenger.send_text(self.session, [text])
        self.assertEqual(sum(isinstance(part, At) for part in self.sent()), 1)

    async def test_hook_and_selected_reply_do_not_duplicate_the_same_quote(self):
        text = self.prepared([Reply(id="source"), Plain("你刚才说的那本书")])
        with self.policy.quote_send(self.session, "source"):
            await self.plugin.messenger.send_text(self.session, [text])
        self.assertEqual(sum(isinstance(part, Reply) for part in self.sent()), 1)

    async def test_metadata_only_hook_chain_is_not_sent_or_receipted(self):
        text = self.prepared([Reply(id="source"), At(qq="42")])
        self.assertFalse(await self.plugin.messenger.send_text(self.session, [text]))
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.messenger.take_sent_texts(self.session), [])
        self.assertFalse(self.plugin.messenger.blocked(self.session))

    async def test_failed_member_read_is_cached_and_does_not_fail_the_send(self):
        self.bot.call_action.side_effect = TimeoutError("metadata unavailable")
        for _ in range(2):
            with self.policy.contact_send(self.session, {"42"}):
                self.assertTrue(await self.plugin.messenger.send_text(self.session, ["最近怎么样"]))
        self.bot.call_action.assert_awaited_once()
        self.assertFalse(self.plugin.messenger.blocked(self.session))

    async def test_unknown_person_never_borrows_the_last_senders_name(self):
        self.plugin._last_events[self.session] = SimpleNamespace(unified_msg_origin=self.session,
            get_sender_id=lambda: "99", get_sender_name=lambda: "另一个群友")
        with self.policy.contact_send(self.session, {"12345"}):
            await self.plugin.messenger.send_text(self.session, ["最近怎么样"])
        text = "".join(part.text for part in self.sent() if isinstance(part, Plain))
        self.assertIn("@12345", text)
        self.assertNotIn("另一个群友", text)

    async def test_reply_hooks_known_name_is_used_when_profile_is_missing(self):
        text = self.prepared([At(qq="12345", name="读书的朋友"), Plain("你刚才说的那本书")])
        await self.plugin.messenger.send_text(self.session, [text])
        self.assertIn("@读书的朋友", "".join(part.text for part in self.sent() if isinstance(part, Plain)))

    async def test_non_onebot_platform_keeps_native_mentions_without_metadata_calls(self):
        self.platform.meta = lambda: SimpleNamespace(name="telegram")
        with self.policy.contact_send(self.session, {"42"}):
            await self.plugin.messenger.send_text(self.session, ["最近怎么样"])
        self.assertEqual(str(next(part for part in self.sent() if isinstance(part, At)).qq), "42")
        self.bot.call_action.assert_not_awaited()

    async def test_member_cache_is_separate_for_groups_and_new_connections(self):
        self.bot.call_action.return_value = {"user_id": 42, "nickname": "阿岚"}
        for target in (self.session, "test:GroupMessage:200"):
            with self.policy.contact_send(target, {"42"}):
                await self.plugin.messenger.send_text(target, ["最近怎么样"])
        self.assertEqual(self.bot.call_action.await_count, 2)
        second = SimpleNamespace(call_action=AsyncMock(return_value={"user_id": 42, "nickname": "新昵称"}))
        self.platform.get_client = lambda: second
        with self.policy.contact_send(self.session, {"42"}):
            await self.plugin.messenger.send_text(self.session, ["最近怎么样"])
        second.call_action.assert_awaited_once()
        self.assertEqual(next(part for part in self.sent() if isinstance(part, At)).name, "新昵称")

    async def test_voice_only_hook_replacement_is_preserved(self):
        voice = SimpleNamespace(type="Record", file="synthetic.wav")
        text = self.prepared([At(qq="42"), voice])
        self.assertTrue(await self.plugin.messenger.send_text(self.session, [text]))
        self.assertIn(voice, self.sent())
        self.assertEqual(self.plugin.messenger.take_sent_texts(self.session), [text])

    async def test_cancellation_during_member_read_never_sends_or_marks_failure(self):
        self.bot.call_action.side_effect = asyncio.CancelledError()
        with self.policy.contact_send(self.session, {"42"}):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.messenger.send_text(self.session, ["最近怎么样"])
        self.plugin.context.send_message.assert_not_awaited()
        self.assertFalse(self.plugin.messenger.blocked(self.session))

    async def test_revoked_permission_during_member_read_prevents_the_send(self):
        allowed = True

        async def read(*_args, **_kwargs):
            nonlocal allowed
            allowed = False
            return {"user_id": 42, "nickname": "阿岚"}

        self.bot.call_action.side_effect = read
        with self.policy.guard_send(lambda *_: allowed), self.policy.contact_send(self.session, {"42"}):
            self.assertFalse(await self.plugin.messenger.send_text(self.session, ["最近怎么样"]))
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.plugin.messenger.take_sent_texts(self.session), [])

    async def test_empty_and_invalid_mention_targets_cannot_ping_anyone(self):
        text = self.prepared([At(qq=""), At(qq="0"), At(qq="unknown"), Plain("你刚才说的那本书")])
        self.assertTrue(await self.plugin.messenger.send_text(self.session, [text]))
        self.assertFalse(any(isinstance(part, At) for part in self.sent()))
        self.bot.call_action.assert_not_awaited()
