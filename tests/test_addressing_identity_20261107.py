"""Message identity and native quote relations, without model or platform calls."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.engine.addressing import AddressingMixin
from core.engine.models import MessageContext
from core.messages import _addressing_kind, _at_targets, _is_at_bot, _reply_target
from core.state import WorldState, chat_event_key


GROUP = "test:GroupMessage:one"
OTHER = "test:GroupMessage:two"


class At:
    def __init__(self, qq, name=""):
        self.qq, self.name = qq, name


class Plain:
    def __init__(self, text):
        self.text = text


class Reply:
    def __init__(self, message_id="", sender_id="", name="", text="", chain=()):
        self.id, self.sender_id, self.sender_nickname = message_id, sender_id, name
        self.message_str, self.chain = text, list(chain)


class Event:
    def __init__(self, chain, *, soft=False):
        self.message_obj = SimpleNamespace(message=chain)
        self.soft = soft

    def get_self_id(self):
        return "bot"

    def get_extra(self, name, default=None):
        return self.soft if name == "intent_router_no_at" else default


class Routing(AddressingMixin):
    world = SimpleNamespace(bot_name="测试角色")

    def _now(self):
        return 1000

    def _chat_origin(self, row, state):
        return str(row.get("origin") or state.session_id)

    def chat_window(self, state, session):
        return list(state.recent_chat)


class AddressingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.engine = Routing()
        self.state = WorldState(GROUP)

    def row(self, mid, *, user="alice", origin=GROUP, text="还好吗", at=1000, **extra):
        value = {"user_id": user, "name": user, "origin": origin, "message_id": mid,
                 "text": text, "at": at, **extra}
        if mid:
            value["event_key"] = chat_event_key(origin, mid)
        self.state.recent_chat.append(value)
        return value

    def test_native_id_only_reference_is_retained_without_inventing_an_author(self):
        reference = _reply_target(Event([Reply("quote-1")]))
        self.assertEqual(reference, {"message_id": "quote-1"})
        self.assertEqual(_addressing_kind([], reference, "bot"), "")

    def test_native_quote_body_survives_missing_archive_and_is_bounded(self):
        reference = _reply_target(Event([Reply("quote-1", text="对 " + "内容" * 500)]))
        self.assertEqual(reference["message_id"], "quote-1")
        self.assertTrue(reference["text"].startswith("对 内容"))
        self.assertEqual(len(reference["text"]), 500)
        self.assertNotIn("is_self", reference)

    def test_native_quote_chain_does_not_promote_its_old_mention_to_a_current_one(self):
        event = Event([Reply("quote-1", "alice", "测试角色同名群友",
                             chain=[At("bot", "测试角色"), Plain("对")])])
        reference = _reply_target(event)
        self.assertEqual(reference["text"], "@测试角色 对")
        self.assertIs(reference["is_self"], False)
        self.assertEqual(_at_targets(event), [])
        self.assertFalse(_is_at_bot(event))
        self.assertEqual(_addressing_kind([], reference, "bot"), "others")

    def test_native_self_quote_uses_sender_identity_without_needing_its_nickname(self):
        reference = _reply_target(Event([Reply("quote-1", "bot", "陌生名片", text="刚回来")]))
        self.assertIs(reference["is_self"], True)
        self.assertEqual(self.engine.reply_addressing(self.state, MessageContext(
            GROUP, user_id="alice", text="那就好", reply_to=reference)), "direct")

    def test_native_quote_facts_survive_archiving_and_protect_the_current_event(self):
        reference = _reply_target(Event([Reply("quote-1", "alice", "阿梨", text="@测试角色 对")]))
        value = self.state.note_chat(user_id="bob", name="小波", text="你俩都没休息吗",
                                     now=1000, origin=GROUP, message_id="m1",
                                     reply_to=reference, addressing="others")
        self.assertEqual(value["reply_to"], reference)
        self.assertEqual(self.engine._mark_reply_addressing(self.state, MessageContext(
            GROUP, user_id="bob", message_id="m1"), "me"), "others")

    def test_native_id_only_quote_survives_archiving_without_becoming_a_sender(self):
        reference = _reply_target(Event([Reply("quote-1", text="原来的那句")]))
        value = self.state.note_chat(user_id="bob", name="小波", text="你俩呢", now=1000,
                                     origin=GROUP, message_id="m1", reply_to=reference)
        self.assertEqual(value["reply_to"]["message_id"], "quote-1")
        self.assertEqual(value["reply_to"]["text"], "原来的那句")
        self.assertFalse(value["reply_to"].get("id"))
        self.assertNotIn("is_self", value["reply_to"])

    def test_router_inserted_mention_is_not_a_real_self_mention(self):
        event = Event([At("bot"), At("alice", "阿梨")], soft=True)
        self.assertFalse(_is_at_bot(event))
        self.assertEqual(_at_targets(event), [{"id": "alice", "name": "阿梨", "self": False}])
        self.assertTrue(_is_at_bot(Event([At("bot")], soft=False)))

    def test_soft_replay_cannot_override_a_current_quote_to_another_person(self):
        reference = _reply_target(Event([Reply("quote-1", "alice", text="对")]))
        self.assertEqual(self.engine.reply_addressing(self.state, MessageContext(
            GROUP, user_id="bob", text="你俩都没休息吗", is_mentioned=True,
            is_soft_wake=True, addressing="others", reply_to=reference)), "interject")

    def test_current_real_self_mention_can_address_me_while_quoting_someone_else(self):
        ctx = MessageContext(GROUP, user_id="bob", text="这个你怎么看", addressing="others",
                             at_targets=[{"id": "bot", "self": True}],
                             reply_to={"message_id": "quote-1", "is_self": False})
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "direct")

    def test_id_only_quote_resolves_the_exact_author_in_this_conversation(self):
        self.row("quote-1", user="__self__", is_self=True)
        ctx = MessageContext(GROUP, user_id="alice", text="那就好", reply_to={"message_id": "quote-1"})
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "direct")
        self.state.recent_chat[0]["is_self"] = False
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "interject")

    def test_quote_to_someone_else_does_not_inherit_their_previous_self_target(self):
        self.row("quote-1", user="alice", addressing="me", at_targets=[{"id": "bot", "self": True}])
        ctx = MessageContext(GROUP, user_id="bob", text="你俩都没休息吗", reply_to={"message_id": "quote-1"})
        self.assertEqual(self.engine._structured_addressing(self.state, ctx), "others")
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "interject")

    def test_unknown_plural_reference_is_unresolved_not_a_claim_that_bot_was_excluded(self):
        self.row("quote-1", user="__self__", origin=OTHER, is_self=True)
        ctx = MessageContext(GROUP, user_id="alice", text="你俩都没休息吗", reply_to={"message_id": "quote-1"})
        self.assertEqual(self.engine._structured_addressing(self.state, ctx), "")
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "interject")

    def test_only_a_real_name_in_current_body_can_claim_an_unresolved_quote(self):
        ctx = MessageContext(GROUP, user_id="alice", raw_text="测试角色，这个你怎么看",
                             reply_to={"message_id": "missing"})
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "direct")
        ctx.raw_text = "你们怎么想"
        ctx.text = "你们怎么想\n［他引用了测试角色的消息］"
        self.assertEqual(self.engine.reply_addressing(self.state, ctx), "interject")

    def test_duplicate_local_quote_ids_are_not_guessed_from_nickname_or_latest_row(self):
        self.row("quote-1", user="__self__", is_self=True)
        self.row("quote-1", user="alice", is_self=False)
        ctx = MessageContext(GROUP, user_id="bob", text="你俩呢", reply_to={"message_id": "quote-1"})
        self.assertEqual(self.engine._structured_addressing(self.state, ctx), "")

    def test_late_verdict_updates_the_same_event_not_the_authors_newer_message(self):
        first, newer = self.row("m1"), self.row("m2", text="今天回来了吗", addressing="others")
        ctx = MessageContext(GROUP, user_id="alice", text="还好吗", message_id="m1")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "me")
        self.assertEqual(first["addressing"], "me")
        self.assertEqual(newer["addressing"], "others")

    def test_shared_state_does_not_mark_the_same_author_and_id_in_another_group(self):
        current = self.row("m1")
        elsewhere = self.row("m1", origin=OTHER, addressing="others")
        ctx = MessageContext(GROUP, user_id="alice", message_id="m1")
        self.engine._mark_reply_addressing(self.state, ctx, "me")
        self.assertEqual(current["addressing"], "me")
        self.assertEqual(elsewhere["addressing"], "others")

    def test_internal_event_identity_is_matched_when_platform_message_id_is_absent(self):
        older = self.row("", event_id="e1")
        newer = self.row("", event_id="e2")
        ctx = MessageContext(GROUP, user_id="alice", event_id="e1")
        self.engine._mark_reply_addressing(self.state, ctx, "others")
        self.assertEqual(older["addressing"], "others")
        self.assertNotIn("addressing", newer)

    def test_missing_or_foreign_event_never_falls_back_to_the_authors_latest_line(self):
        value = self.row("m1")
        ctx = MessageContext(GROUP, user_id="alice", message_id="absent")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "")
        ctx.message_id, ctx.user_id = "m1", "bob"
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "")
        self.assertNotIn("addressing", value)

    def test_native_non_self_quote_keeps_its_target_when_model_says_me(self):
        self.row("quote-1", user="alice", addressing="me")
        value = self.row("m1", user="bob", reply_to={"message_id": "quote-1"}, addressing="others")
        ctx = MessageContext(GROUP, user_id="bob", message_id="m1")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "others")
        self.assertEqual(value["addressing"], "others")

    def test_real_self_mention_is_also_preserved_against_an_inconsistent_model_label(self):
        value = self.row("m1", at_targets=[{"id": "bot", "self": True}], addressing="me",
                         reply_to={"message_id": "quote-1", "is_self": False})
        ctx = MessageContext(GROUP, user_id="alice", message_id="m1")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "others"), "me")
        self.assertEqual(value["addressing"], "me")

    def test_unissued_legacy_identity_requires_a_unique_current_author_and_body(self):
        self.row("", text="重复的话")
        self.row("", text="重复的话")
        ctx = MessageContext(GROUP, user_id="alice", text="重复的话")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "")
        self.assertTrue(all("addressing" not in item for item in self.state.recent_chat))

    def test_unique_legacy_body_works_but_old_body_is_not_promoted(self):
        value = self.row("", text="近来的原话")
        self.row("", text="另一句话")
        ctx = MessageContext(GROUP, user_id="alice", text="近来的原话")
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "others"), "others")
        value["at"] = 800
        self.assertEqual(self.engine._mark_reply_addressing(self.state, ctx, "me"), "")
        self.assertEqual(value["addressing"], "others")


if __name__ == "__main__":
    unittest.main()
