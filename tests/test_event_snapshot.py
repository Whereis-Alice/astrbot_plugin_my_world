"""Snapshots carry message data, not a completed pipeline's asynchronous handles."""
import asyncio
import copy
import importlib
import unittest
from types import SimpleNamespace

from tests import test_dialogue_inject as fixture

media = importlib.import_module(fixture.MODULE.__name__ + ".adapters.proactive_media")
native = importlib.import_module(fixture.MODULE.__name__ + ".adapters.proactive_context")


class Event(fixture.Event):
    def clear_result(self):
        self._result = None


class EventSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def event(self):
        event = Event("本轮原文", session="test:GroupMessage:a")
        event._extras = {"plugin_facts": {"items": ["世界书资料"]}}
        return event

    async def test_runtime_request_is_removed_before_copying_original_extras(self):
        future = asyncio.get_running_loop().create_future()
        event = self.event()
        old_request = SimpleNamespace(owner=future)
        event._extras.update(provider_request=old_request, selected_model=future,
                             my_world_reply_owner=future, mw_dialogue_turn={"owner": future})
        result = native.isolated_event(event, SimpleNamespace(get_config=lambda _: {}), event.unified_msg_origin)
        self.assertEqual(result._extras["plugin_facts"], event._extras["plugin_facts"])
        self.assertNotIn("provider_request", result._extras)
        self.assertNotIn("mw_dialogue_turn", result._extras)
        self.assertIs(event._extras["provider_request"], old_request)
        self.assertFalse(future.cancelled())

    async def test_uncopyable_plugin_handle_does_not_discard_the_message_or_other_data(self):
        future = asyncio.get_running_loop().create_future()
        event = self.event()
        event._extras["plugin_runtime"] = {"handle": future}
        result = media.snapshot_event(event)
        self.assertNotIn("plugin_runtime", result._extras)
        result._extras["plugin_facts"]["items"].append("仅限本轮")
        self.assertEqual(event._extras["plugin_facts"]["items"], ["世界书资料"])
        self.assertEqual(result.message_obj.message_id, event.message_obj.message_id)
        self.assertFalse(future.done())

    async def test_media_cache_keeps_exact_event_despite_an_old_provider_request(self):
        event = self.event()
        event._extras["provider_request"] = SimpleNamespace(owner=asyncio.get_running_loop().create_future())
        cache = media.ProactiveMedia(SimpleNamespace())
        cache.remember(event, {"message_id": "source"})
        snapshot = cache.events[(event.unified_msg_origin, "source")][1]
        self.assertEqual(snapshot.message_obj.message_id, event.message_obj.message_id)
        self.assertNotIn("provider_request", snapshot._extras)

    async def test_public_request_copies_content_and_tool_list_without_cloning_live_tools(self):
        snapshot_module = importlib.import_module(fixture.MODULE.__name__ + ".adapters.event_snapshot")
        future = asyncio.get_running_loop().create_future()
        tool = SimpleNamespace(name="lookup", owner=future)
        request = SimpleNamespace(func_tool=SimpleNamespace(tools=[tool]),
            contexts=[{"role": "user", "content": "真实历史"}], image_urls=["original.png"],
            conversation=SimpleNamespace(history="[]"))
        with self.assertRaises(TypeError):
            copy.deepcopy(request)
        result = snapshot_module.snapshot_request(request)
        self.assertIs(result.func_tool.tools[0], tool)
        self.assertIsNot(result.func_tool.tools, request.func_tool.tools)
        result.func_tool.tools.clear()
        result.contexts[0]["content"] = "本轮修改"
        result.image_urls.clear()
        self.assertEqual(request.contexts[0]["content"], "真实历史")
        self.assertEqual(request.image_urls, ["original.png"])
        self.assertEqual(len(request.func_tool.tools), 1)
        self.assertFalse(future.done())
