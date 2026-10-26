"""Accepted proactive segments must keep the branch created by their own batch."""
import asyncio
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_web_routes import load_plugin_module


class ProactiveHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delivery = importlib.import_module(load_plugin_module().__name__ + ".adapters.proactive_delivery")
        self.target = "synthetic:GroupMessage:room"
        self.current = None
        self.histories = {}
        self.lock = asyncio.Lock()

        async def create(*_args):
            self.current = "created"
            self.histories[self.current] = []
            return self.current

        async def update(_target, cid, *, history):
            self.histories[cid] = history

        self.manager = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(side_effect=lambda _: self.current),
            get_conversation=AsyncMock(side_effect=lambda _target, cid: types.SimpleNamespace(
                cid=cid, history=json.dumps(self.histories[cid]))),
            new_conversation=AsyncMock(side_effect=create), update_conversation=AsyncMock(side_effect=update))
        self.plugin = types.SimpleNamespace(context=types.SimpleNamespace(conversation_manager=self.manager),
                                            engine=types.SimpleNamespace())
        lock_module = types.ModuleType("astrbot.core.utils.session_lock")
        lock_module.session_lock_manager = types.SimpleNamespace(acquire_lock=lambda _: self.lock)
        response_module = types.ModuleType("astrbot.core.provider.entities")
        response_module.LLMResponse = types.SimpleNamespace
        stack = patch.dict(sys.modules, {lock_module.__name__: lock_module, response_module.__name__: response_module})
        stack.start()
        self.addCleanup(stack.stop)
        hook = patch.object(self.delivery, "fire_hook", AsyncMock(return_value=False))
        self.hook = hook.start()
        self.addCleanup(hook.stop)

    def wire(self, expected=None):
        history_target = {"cid": expected}
        rows = {}
        for index, text in enumerate(("第一段", "第二段")):
            request = types.SimpleNamespace(conversation=types.SimpleNamespace(cid=expected or ""))
            event = types.SimpleNamespace(get_extra=lambda _, request=request: request,
                                          get_platform_id=lambda: "synthetic")
            rows[(self.target, index)] = {"text": text, "event": event, "accepted": False,
                                         "history_target": history_target}
        return rows

    async def accept(self, index):
        await self.delivery.accepted(self.plugin, self.target, ("第一段", "第二段")[index], index)

    async def test_new_branch_remembers_all_segments_once_without_mutating_request(self):
        rows = self.wire()
        token = self.delivery.WIRE.set(rows)
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        await self.accept(0)
        await self.accept(1)
        self.assertEqual(self.histories["created"], [
            {"role": "assistant", "content": "第一段"}, {"role": "assistant", "content": "第二段"}])
        self.manager.new_conversation.assert_awaited_once()
        self.assertEqual(self.manager.update_conversation.await_count, 2)
        self.assertEqual(self.hook.await_count, 4)
        self.assertTrue(all(row["event"].get_extra("provider_request").conversation.cid == "" for row in rows.values()))

    async def test_interleaved_user_reply_is_preserved_before_later_segment(self):
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        self.histories["created"].append({"role": "user", "content": "然后呢？"})
        await self.accept(1)
        self.assertEqual([row["content"] for row in self.histories["created"]], ["第一段", "然后呢？", "第二段"])

    async def test_existing_branch_keeps_previous_history_and_every_segment(self):
        self.current = "existing"
        self.histories[self.current] = [{"role": "user", "content": "原来的话"}]
        token = self.delivery.WIRE.set(self.wire(self.current))
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        await self.accept(1)
        self.assertEqual([row["content"] for row in self.histories["existing"]], ["原来的话", "第一段", "第二段"])
        self.manager.new_conversation.assert_not_awaited()

    async def test_switch_or_reset_after_first_segment_never_writes_later_segment(self):
        for current in ("switched", None):
            with self.subTest(current=current):
                self.current, self.histories = None, {}
                rows = self.wire()
                token = self.delivery.WIRE.set(rows)
                try:
                    await self.accept(0)
                    self.current = current
                    if current:
                        self.histories[current] = []
                    await self.accept(1)
                    self.assertEqual([row["content"] for row in self.histories["created"]], ["第一段"])
                    if current:
                        self.assertEqual(self.histories[current], [])
                finally:
                    self.delivery.WIRE.reset(token)

    async def test_independent_stale_batch_cannot_attach_to_branch_created_by_another(self):
        first, stale = self.wire(), self.wire()
        token = self.delivery.WIRE.set(first)
        try:
            await self.accept(0)
            self.delivery.WIRE.set(stale)
            await self.accept(1)
            self.assertEqual([row["content"] for row in self.histories["created"]], ["第一段"])
        finally:
            self.delivery.WIRE.reset(token)

    async def test_public_hook_clearing_request_cannot_bypass_branch_switch_guard(self):
        rows = self.wire()
        token = self.delivery.WIRE.set(rows)
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        self.current = "switched"
        self.histories[self.current] = []
        rows[(self.target, 1)]["event"].get_extra = lambda _: None
        await self.accept(1)
        self.assertEqual(self.histories["switched"], [])
        self.assertEqual([row["content"] for row in self.histories["created"]], ["第一段"])

    async def test_transient_write_failure_recovers_before_memory_hooks_without_resending(self):
        update = self.manager.update_conversation
        failed = False

        async def fail_once(*args, **kwargs):
            nonlocal failed
            self.assertTrue(self.lock.locked())
            if not failed:
                failed = True
                raise OSError("temporary database failure")
            await update(*args, **kwargs)

        self.manager.update_conversation = AsyncMock(side_effect=fail_once)
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        await self.accept(0)
        self.assertEqual(self.histories["created"], [{"role": "assistant", "content": "第一段"}])
        self.assertEqual(self.manager.update_conversation.await_count, 2)
        self.assertEqual(self.hook.await_count, 2)

    async def test_committed_write_error_does_not_duplicate_identical_older_text(self):
        self.current = "existing"
        self.histories[self.current] = [{"role": "assistant", "content": "第一段"}]
        update = self.manager.update_conversation

        async def commit_then_fail(*args, **kwargs):
            await update(*args, **kwargs)
            raise OSError("write response lost after commit")

        self.manager.update_conversation = AsyncMock(side_effect=commit_then_fail)
        token = self.delivery.WIRE.set(self.wire(self.current))
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        await self.accept(0)
        self.assertEqual(self.histories["existing"], [{"role": "assistant", "content": "第一段"}] * 2)
        self.manager.update_conversation.assert_awaited_once()
        self.assertEqual(self.hook.await_count, 2)

    async def test_persistent_history_failure_still_notifies_memory_once(self):
        failure = OSError("persistent database failure")
        self.manager.update_conversation = AsyncMock(side_effect=failure)
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        with self.assertRaises(OSError) as caught:
            await self.accept(0)
        self.assertIs(caught.exception, failure)
        await self.accept(0)
        self.assertEqual(self.hook.await_count, 2)
        self.assertEqual(self.hook.await_args_list[0].args[2], "OnLLMResponseEvent")
        self.assertTrue(self.hook.await_args_list[0].kwargs["accepted"])
        self.assertEqual(self.hook.await_args_list[1].args[2], "OnAfterMessageSentEvent")
        self.assertEqual(self.manager.update_conversation.await_count, 2)
        self.assertEqual(self.histories["created"], [])

    async def test_write_failure_with_branch_switch_does_not_retry_into_new_branch(self):
        async def switch_and_fail(*args, **kwargs):
            self.current = "switched"
            self.histories[self.current] = [{"role": "user", "content": "新会话"}]
            raise OSError("database failed while branch changed")

        self.manager.update_conversation = AsyncMock(side_effect=switch_and_fail)
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        await self.accept(0)
        self.manager.update_conversation.assert_awaited_once()
        self.assertEqual(self.histories["switched"], [{"role": "user", "content": "新会话"}])
        self.assertEqual(self.hook.await_count, 2)

    async def test_changed_history_after_failed_write_is_preserved_without_blind_retry(self):
        failure = OSError("database failed while history changed")

        async def edit_and_fail(*args, **kwargs):
            self.histories[self.current] = [{"role": "user", "content": "手动编辑过的历史"}]
            raise failure

        self.manager.update_conversation = AsyncMock(side_effect=edit_and_fail)
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        with self.assertRaises(OSError) as caught:
            await self.accept(0)
        self.assertIs(caught.exception, failure)
        self.manager.update_conversation.assert_awaited_once()
        self.assertEqual(self.histories["created"], [{"role": "user", "content": "手动编辑过的历史"}])
        self.assertEqual(self.hook.await_count, 2)

    async def test_history_write_cancellation_propagates_without_retrying_hooks(self):
        self.manager.update_conversation = AsyncMock(side_effect=asyncio.CancelledError())
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        with self.assertRaises(asyncio.CancelledError):
            await self.accept(0)
        self.manager.update_conversation.assert_awaited_once()
        self.hook.assert_not_awaited()

    async def test_failed_readback_preserves_write_error_and_still_notifies_memory(self):
        failure = OSError("history write failed")
        self.manager.update_conversation = AsyncMock(side_effect=failure)
        read = self.manager.get_conversation

        async def fail_readback(*args, **kwargs):
            if self.manager.update_conversation.await_count:
                raise ValueError("readback failed too")
            return await read(*args, **kwargs)

        self.manager.get_conversation = AsyncMock(side_effect=fail_readback)
        token = self.delivery.WIRE.set(self.wire())
        self.addCleanup(self.delivery.WIRE.reset, token)
        with self.assertRaises(OSError) as caught:
            await self.accept(0)
        self.assertIs(caught.exception, failure)
        self.manager.update_conversation.assert_awaited_once()
        self.assertEqual(self.hook.await_count, 2)
