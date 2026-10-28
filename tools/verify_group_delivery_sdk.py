"""Check real AstrBot/OneBot serialization without calling a live transport."""
import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain, Record, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

package = ModuleType("world_group_delivery_sdk_check")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
delivery = importlib.import_module(package.__name__ + ".adapters.group_mentions")


async def main():
    target = "synthetic:GroupMessage:1234"
    action = AsyncMock(return_value={"user_id": 42, "nickname": "", "card": ""})
    bot = SimpleNamespace(call_action=action)
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: bot)
    plugin = SimpleNamespace(context=SimpleNamespace(get_platform_inst=lambda _: platform),
        last_event=lambda _: None, engine=SimpleNamespace(profiles=SimpleNamespace(
            profile=lambda sid, uid: {"payload": {"names": ["阿岚"]}} if (sid, uid) == (target, "42") else None)))
    resolver = delivery.GroupMentions(plugin)
    original = delivery.addressed_chain([At(qq="42", name="阿岚"), Reply(id="7"), Plain(text="你刚才说的那本书")],
                                        ("42",), "7")
    assert sum(isinstance(part, At) for part in original) == 1
    assert sum(isinstance(part, Reply) for part in original) == 1
    assert "name" not in original[0].toDict()["data"]  # The native serializer drops it.
    prepared = await resolver.prepare(target, original)
    payload = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain=prepared))
    assert not any(part["type"] == "at" for part in payload)
    assert "@阿岚" in "".join(part["data"]["text"] for part in payload if part["type"] == "text")
    assert original[0].name == "阿岚"  # Never mutate the hook's chain.
    action.assert_awaited_once_with("get_group_member_info", group_id=1234, user_id=42, no_cache=False)

    resolver.cache.clear()
    action.return_value = {"user_id": 42, "nickname": "阿岚", "card": "读书的阿岚"}
    prepared = await resolver.prepare(target, original)
    payload = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain=prepared))
    assert [part["data"]["qq"] for part in payload if part["type"] == "at"] == ["42"]
    assert next(part for part in prepared if isinstance(part, At)).name == "读书的阿岚"
    assert delivery.has_body([Record(file="not-sent.wav")])
    assert not delivery.has_body([At(qq="42"), Reply(id="7"), Plain(text=" ")])

    resolver.cache.clear()
    action.side_effect = TimeoutError("synthetic metadata failure")
    prepared = await resolver.prepare(target, [At(qq="12345"), Plain(text="最近怎么样")])
    assert any(isinstance(part, Plain) and part.text == "@12345 " for part in prepared)
    assert not any(isinstance(part, At) for part in prepared)
    print("6 AstrBot SDK checks passed; no live provider or transport called")


if __name__ == "__main__":
    asyncio.run(main())
