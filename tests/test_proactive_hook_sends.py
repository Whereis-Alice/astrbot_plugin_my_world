"""Eager decorator segments stay gated, attributed and local to their draft."""
import asyncio
import copy
import importlib
import sys
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_proactive_delivery_identity as identity
from tests.test_group_delivery import Plain


OUTPUT_MODULE = "data.plugins.astrbot_plugin_outputpro_split.main"


class Config:
    __module__ = OUTPUT_MODULE

    def __init__(self, context):
        self.context = context


class Step:
    __module__ = OUTPUT_MODULE

    def __init__(self, config, callback):
        self.plugin_config, self.callback = config, callback

    async def handle(self, event):
        await self.callback(self, event)


class Pipeline:
    __module__ = OUTPUT_MODULE

    def __init__(self, config, callback):
        self.plugin_config = config
        self._steps = [Step(config, callback)]
        self.lock = asyncio.Lock()

    async def run(self, event):
        await self._steps[0].handle(event)


class OutputPlugin:
    __module__ = OUTPUT_MODULE

    def __init__(self, context, callback):
        self.context, self.cfg = context, Config(context)
        self.pipeline = Pipeline(self.cfg, callback)

    async def on_decorating_result(self, event):
        await self.pipeline.run(event)


OutputPlugin.on_decorating_result.__module__ = OUTPUT_MODULE


class Event(identity.HookEvent):
    stopped = False
    plugins_name = None

    def is_stopped(self):
        return self.stopped


class ProactiveHookSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await identity.ProactiveDeliveryIdentityTests.asyncSetUp(self)
        self.parts = importlib.import_module(self.module.__name__ + ".adapters.group_mentions")
    two_contacts = identity.ProactiveDeliveryIdentityTests.two_contacts

    @contextmanager
    def environment(self, callback, transport=None):
        original = Event(self.session)
        if transport is not None:
            original.bot = transport
        self.plugin.last_event = lambda _: original
        self.plugin.context.tool_loop_agent = AsyncMock()
        owner = OutputPlugin(self.plugin.context, callback)
        components = types.ModuleType("astrbot.core.message.components")
        components.Plain = Plain
        results = types.ModuleType("astrbot.core.message.message_event_result")
        results.MessageEventResult = identity.HookResult
        results.ResultContentType = SimpleNamespace(LLM_RESULT="llm")
        registry = types.ModuleType("astrbot.core.star.star_handler")
        registry.EventType = SimpleNamespace(OnLLMResponseEvent="response", OnDecoratingResultEvent="decorate",
                                             OnAfterMessageSentEvent="sent")
        metadata = SimpleNamespace(handler=owner.on_decorating_result, handler_name="output_split")
        registry.star_handlers_registry = SimpleNamespace(get_handlers_by_event_type=
            lambda name, **_: [metadata] if name == "decorate" else [])

        def response(text, raw=None):
            return SimpleNamespace(completion_text=text, result_chain=SimpleNamespace(chain=[Plain(text)]))

        with patch.dict(sys.modules, {components.__name__: components, results.__name__: results,
                                      registry.__name__: registry}), \
                patch.object(self.delivery, "isolated_event", side_effect=lambda event, *_: copy.deepcopy(event)), \
                patch.object(self.delivery, "public_response", side_effect=response), \
                patch.object(self.delivery, "append_history", AsyncMock()) as history:
            yield owner, history

    def one_draft(self):
        view = self.two_contacts()
        view.messages = [view.messages[0]]
        for name in ("speech_kinds", "speech_ids", "speech_people", "speech_origins", "speech_proofs"):
            getattr(view, name)[""] = getattr(view, name).get("", [])[:1]
        view.speech_decisions = view.speech_decisions[:1]
        text = "第一句。第二句。"
        view.messages[0] = text
        view.speech_decisions[0]["drafts"] = [text]
        return view

    async def split(self, step, event):
        await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[Plain("第一句")] ))
        event.get_result().chain = [Plain("第二句")]

    def sent_bodies(self):
        return ["".join(part.text for part in call.args[1].chain if isinstance(part, Plain))
                for call in self.plugin.context.send_message.await_args_list]

    async def test_eager_segments_are_sent_once_with_one_decision_and_real_receipts(self):
        view = self.one_draft()
        with self.environment(self.split) as (owner, history):
            await self.plugin.engine._deliver(view)
            self.assertIs(owner.context, self.plugin.context)
            self.assertIs(owner.pipeline.plugin_config, owner.cfg)
            self.assertIs(owner.pipeline._steps[0].plugin_config.context, self.plugin.context)
            self.assertEqual(history.await_count, 2)
        self.assertEqual(len(self.sent_bodies()), 2)
        self.assertTrue(self.sent_bodies()[0].endswith("第一句"))
        self.assertTrue(self.sent_bodies()[1].endswith("第二句"))
        self.assertEqual(view.speech_decisions[0]["drafts"], ["第一句", "第二句"])
        self.assertEqual(view.speech_decisions[0]["sent"], ["第一句", "第二句"])
        self.assertEqual(view.speech_decisions[0]["status"], "sent")
        self.assertEqual(len(view.accepted_receipts), 2)
        self.assertEqual(len({row["identity"] for row in view.accepted_receipts.values()}), 1)
        self.assertEqual(self.plugin.engine.contact_status(self.session, "111")["person_left"], 1)

    async def test_direct_reply_arriving_during_hook_cancels_every_staged_segment(self):
        view = self.one_draft()

        async def interrupted(step, event):
            await self.split(step, event)
            self.plugin.engine.reply_busy = lambda _: True

        with self.environment(interrupted) as (_, history):
            await self.plugin.engine._deliver(view)
            history.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()
        self.assertFalse(view.speech_decisions[0]["sent"])
        self.assertFalse(view.accepted_receipts)

    async def test_direct_reply_between_segments_keeps_only_first_actual_receipt(self):
        view = self.one_draft()

        async def accept_then_interrupt(*_):
            self.plugin.engine.reply_busy = lambda _: True
            return True

        self.plugin.context.send_message.side_effect = accept_then_interrupt
        with self.environment(self.split) as (_, history):
            await self.plugin.engine._deliver(view)
            self.assertEqual(history.await_count, 1)
        self.plugin.context.send_message.assert_awaited_once()
        self.assertEqual(view.speech_decisions[0]["sent"], ["第一句"])
        self.assertEqual(view.speech_decisions[0]["status"], "partial")
        self.assertEqual(len(view.accepted_receipts), 1)

    async def test_stop_discards_previously_staged_segments(self):
        async def stop(step, event):
            await self.split(step, event)
            event.stopped = True

        view = self.one_draft()
        with self.environment(stop) as (_, history):
            await self.plugin.engine._deliver(view)
            history.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()
        self.assertFalse(view.accepted_receipts)

    async def test_exception_discards_previously_staged_segments(self):
        async def fail(step, event):
            await self.split(step, event)
            raise RuntimeError("decorator failed")

        view = self.one_draft()
        with self.environment(fail) as (_, history):
            await self.plugin.engine._deliver(view)
            history.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()
        self.assertFalse(view.accepted_receipts)

    async def test_normal_concurrent_handler_keeps_original_context_and_pipeline(self):
        entered, release = asyncio.Event(), asyncio.Event()
        view = self.one_draft()

        async def pause(step, event):
            if event.message_str == "普通回复":
                await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[Plain("普通前段")]))
                return
            await self.split(step, event)
            entered.set()
            await release.wait()

        with self.environment(pause) as (owner, _):
            task = asyncio.create_task(self.plugin.engine._deliver(view))
            await asyncio.wait_for(entered.wait(), 2)
            normal = Event(self.session)
            normal.message_str = "普通回复"
            await asyncio.create_task(owner.on_decorating_result(normal))
            self.assertEqual(self.sent_bodies(), ["普通前段"])
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.sent_bodies()), 3)

    async def test_child_task_and_expired_proxy_cannot_send_the_old_draft(self):
        scope = importlib.import_module(self.module.__name__ + ".adapters.proactive_hook_sends")
        child_errors, proxies = [], []

        async def child(step, event):
            proxies.append(step.plugin_config.context)
            async def send_child():
                try:
                    await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[Plain("旧稿")]))
                except RuntimeError:
                    child_errors.append(True)
            await asyncio.create_task(send_child())
            await self.split(step, event)

        view = self.one_draft()
        with self.environment(child):
            async with self.delivery.delivery(self.plugin, self.session, view):
                self.plugin.context.send_message.assert_not_awaited()
                with self.assertRaises(RuntimeError):
                    await proxies[0].send_message(self.session, SimpleNamespace(chain=[Plain("过期稿")]))
        self.assertEqual(child_errors, [True])
        self.assertEqual(view.messages, [])
        self.assertIsNone(scope.HOOK_SENDS.get())
        self.plugin.context.send_message.assert_not_awaited()

    async def test_image_components_survive_in_both_captured_first_and_final_segment(self):
        from astrbot.api.message_components import Image
        first, tail = Image(file="first.png"), Image(file="tail.png")

        async def images(step, event):
            await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[Plain("第一句"), first]))
            event.get_result().chain = [Plain("第二句"), tail]

        with self.environment(images):
            await self.plugin.engine._deliver(self.one_draft())
        calls = self.plugin.context.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([self.parts._field(part, "file") for part in calls[0].args[1].chain
                          if self.parts._kind(part) == "image"], ["first.png"])
        self.assertEqual([self.parts._field(part, "file") for part in calls[1].args[1].chain
                          if self.parts._kind(part) == "image"], ["tail.png"])

    async def test_image_prefix_keeps_media_metadata_without_repeating_the_tail_words(self):
        from astrbot.api.message_components import Image
        image = Image(file="base64://" + "A" * 2000)

        async def prefix(step, event):
            await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[image]))
            event.get_result().chain = [Plain("尾句")]

        view = self.one_draft()
        with self.environment(prefix) as (_, history):
            await self.plugin.engine._deliver(view)
            self.assertEqual([call.args[3] for call in history.await_args_list], ["［我发了一张图片］", "尾句"])
        state = await self.plugin.engine.load_state(self.session, cold_start=False)
        rows = [row for row in state.recent_chat if row.get("is_self")]
        self.assertEqual([row["text"] for row in rows], ["［我发了一张图片］", "尾句"])
        self.assertTrue(rows[0]["has_images"])
        self.assertEqual(rows[0]["image_count"], 1)
        self.assertTrue(rows[0]["images"][0].startswith("sha256:"))
        self.assertEqual({row["speech_turn"] for row in rows}, {view.speech_receipt_batch})
        self.assertFalse(rows[1].get("has_images"))
        self.assertNotIn("第一句。第二句。", str(rows))

    async def test_raw_image_summary_keeps_exact_payload_through_capture_copy_and_wire(self):
        payload = [{"type": "image", "data": {"file": "base64://" + "B" * 1200,
                                                "summary": "外显金句", "sub_type": 0}}]
        transport = SimpleNamespace(send=AsyncMock(return_value=True))

        async def summary(_step, event):
            await event.bot.send({"group_id": event.get_group_id()}, payload)
            event.get_result().chain = []

        view = self.one_draft()
        with self.environment(summary, transport):
            await self.plugin.engine._deliver(view)
        # The real OneBot adapter serializes this opaque prepared component
        # through toDict(), preserving fields absent from native Image.
        sent = self.plugin.context.send_message.await_args.args[1].chain
        images = [part for part in sent if self.parts._kind(part) == "image"]
        self.assertEqual([part.toDict() for part in images], payload)
        self.assertEqual(copy.deepcopy(images[0]).toDict(), payload[0])
        self.assertEqual(view.speech_decisions[0]["sent"], ["［我发了一张图片］"])
        self.assertNotIn("外显金句", str(view.accepted_receipts))
        self.assertTrue(next(iter(view.accepted_receipts.values()))["images"][0].startswith("sha256:"))
        transport.send.assert_not_awaited()

    async def test_raw_image_summary_cannot_redirect_a_different_group_to_this_room(self):
        transport = SimpleNamespace(send=AsyncMock())

        async def cross_room(_step, event):
            await event.bot.send({"group_id": "other"}, [{"type": "image", "data": {"file": "x.png"}}])
            event.get_result().chain = []

        with self.environment(cross_room, transport):
            await self.plugin.engine._deliver(self.one_draft())
        self.plugin.context.send_message.assert_not_awaited()
        transport.send.assert_not_awaited()

    async def test_voice_only_replacement_retains_the_actual_spoken_source(self):
        async def voice(_step, event):
            event.get_result().chain = [{"type": "record", "file": "voice.mp3"}]

        view = self.one_draft()
        with self.environment(voice):
            await self.plugin.engine._deliver(view)
        self.assertEqual(view.speech_decisions[0]["sent"], ["第一句。第二句。"])

    async def test_expansion_keeps_multiple_drafts_debug_positions_and_rebase_identity(self):
        view = self.two_contacts()
        view.debug_messages, view.debug_positions = ["debug"], [1]

        async def numbered(step, event):
            text = event.get_result().chain[0].text
            await step.plugin_config.context.send_message(self.session, SimpleNamespace(chain=[Plain(text + "前")]))
            event.get_result().chain = [Plain(text + "后")]

        with self.environment(numbered):
            async with self.delivery.delivery(self.plugin, self.session, view):
                self.assertEqual(view.speech_ids[""], ["111", "111", "222", "222"])
                self.assertEqual(view.debug_positions, [2])
                self.assertIsNone(self.delivery.prepared_chain(self.session, "debug", 2))
                self.assertIsNotNone(self.delivery.prepared_chain(self.session, view.messages[2], 3))
                retained = [1, 2, 3]
                view.messages = [view.messages[i] for i in retained]
                view.debug_positions = [1]
                self.delivery.rebase_prepared(view, retained)
                chain = self.delivery.prepared_chain(self.session, view.messages[0], 0)
                self.assertTrue(chain[0].text.endswith("后"))
                self.assertIsNone(self.delivery.prepared_chain(self.session, "debug", 1))
                self.assertIsNotNone(self.delivery.prepared_chain(self.session, view.messages[1], 2))
        self.plugin.context.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
