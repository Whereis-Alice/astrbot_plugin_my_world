"""世界生成（区域 / 日程 / 事件）：解析器与提示词的纯逻辑测试。

生成结果永远只是**草稿**：解析器负责挡住模型瞎编的 id、重复的 id、
不存在的动作与地点，再把干净的草稿交给编辑器让用户改。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.engine import MyWorldEngine  # noqa: E402
from core.generator import (  # noqa: E402
    auto_layout_ring,
    connect_zone_graph,
    clamp_event_count,
    clamp_map_nodes_per_zone,
    clamp_map_zone_count,
    clamp_schedule_count,
    clamp_world_action_count,
    clamp_zone_count,
    parse_generated_actions,
    parse_generated_events,
    parse_generated_schedules,
    parse_generated_zones,
    parse_generated_zone_pack,
    parse_generated_world_map,
    spring_layout_zones,
)
from core.models import parse_world  # noqa: E402
from core.ports import LLMReply  # noqa: E402
from core.prompt import PromptBuilder  # noqa: E402
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:9001"


class ZoneParserTests(unittest.TestCase):
    def test_keeps_clean_zones_and_dedupes_the_home(self):
        text = (
            '{"zones": ['
            '{"id": "old_bookstore", "name": "旧书店", "note": "她小时候常来", '
            '"icon": "📚", "color": "#8B7DD8", "is_home": true},'
            '{"id": "old_bookstore", "name": "重复的", "is_home": false},'
            '{"id": "riverside", "name": "河边", "note": "适合发呆", "is_home": true}'
            "]}"
        )
        zones, problems = parse_generated_zones(
            text, existing_ids={"home"}, max_zones=3
        )
        self.assertEqual([zone["id"] for zone in zones], ["old_bookstore", "riverside"])
        self.assertTrue(zones[0]["is_home"])
        self.assertFalse(zones[1]["is_home"], "一个世界只认一个家")
        self.assertTrue(any("重复" in item for item in problems))
        self.assertTrue(any("只保留第一个" in item for item in problems))

    def test_rejects_bad_ids_and_survives_garbage(self):
        text = '{"zones": [{"id": "!!!", "name": "Bad"}, "x", 3]}'
        zones, problems = parse_generated_zones(text)
        self.assertEqual(zones, [])
        self.assertTrue(problems)

    def test_messy_but_usable_ids_are_slugged(self):
        text = '{"zones": [{"id": "Good Zone", "name": "好地方"}]}'
        zones, problems = parse_generated_zones(text)
        self.assertEqual([zone["id"] for zone in zones], ["good_zone"])
        self.assertEqual(problems, [])


class WorldActionParserTests(unittest.TestCase):
    def test_total_limit_and_existing_ids_are_respected(self):
        text = (
            '{"actions": ['
            '{"id": "water_flowers", "node_id": "n1", "name": "给花浇水"},'
            '{"id": "water_flowers", "node_id": "n1", "name": "重复的"},'
            '{"id": "watch_rain", "node_id": "n1", "name": "看雨"}'
            "]}"
        )
        actions, problems = parse_generated_actions(
            text, node_ids=["n1"], max_total=1
        )
        self.assertEqual([item["id"] for item in actions], ["water_flowers"])
        self.assertTrue(any("上限" in item for item in problems))

        actions, problems = parse_generated_actions(
            text,
            node_ids=["n1"],
            existing_ids={"water_flowers"},
            max_total=5,
        )
        self.assertEqual([item["id"] for item in actions], ["watch_rain"])
        self.assertTrue(any("重复" in item for item in problems))


class ScheduleParserTests(unittest.TestCase):
    def test_drops_steps_that_point_at_missing_actions_or_nodes(self):
        text = (
            '{"schedules": [{"id": "morning", "time": "08:30", "days": ["mon", "mon", "monday"], '
            '"note": "起床", "smart": true, "action_chain": ['
            '{"type": "walk_to", "target_node": "kitchen"},'
            '{"type": "walk_to", "target_node": "现在不存在的地方"},'
            '{"type": "cook", "intent": "做个早饭"},'
            '{"type": "launch_rocket", "intent": "不能做"}'
            "]}]}"
        )
        schedules, problems = parse_generated_schedules(
            text,
            existing_ids=set(),
            action_ids={"cook", "walk_to"},
            node_ids={"kitchen", "bedroom"},
            max_schedules=2,
        )
        self.assertEqual(len(schedules), 1)
        schedule = schedules[0]
        self.assertEqual(schedule["time"], "08:30")
        self.assertEqual(schedule["days"], ["mon"])
        self.assertEqual(
            [step["type"] for step in schedule["action_chain"]], ["walk_to", "cook"]
        )
        self.assertTrue(any("不存在" in item for item in problems))
        self.assertTrue(any("不可用" in item for item in problems))

    def test_invalid_time_is_skipped_with_a_reason(self):
        text = '{"schedules": [{"id": "bad", "time": "25:99", "action_chain": [{"type": "say"}]}]}'
        schedules, problems = parse_generated_schedules(
            text, action_ids={"say"}, node_ids=set()
        )
        self.assertEqual(schedules, [])
        self.assertTrue(any("不合法" in item for item in problems))

    def test_id_collision_is_skipped(self):
        text = '{"schedules": [{"id": "morning", "time": "08:00", "action_chain": [{"type": "say"}]}]}'
        schedules, problems = parse_generated_schedules(
            text, existing_ids={"morning"}, action_ids={"say"}
        )
        self.assertEqual(schedules, [])
        self.assertTrue(any("重复" in item for item in problems))


class EventParserTests(unittest.TestCase):
    def test_accepts_dicts_and_bare_strings(self):
        text = (
            '{"events": ['
            '{"title": "旧朋友来信", "text": "她收到一封很多年没联系的人寄来的信。"},'
            '"楼下新开了一家店"'
            "]}"
        )
        events, problems = parse_generated_events(text, max_events=3)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["title"], "旧朋友来信")
        self.assertIn("信", events[0]["text"])
        self.assertTrue(events[1]["text"].startswith("楼下"))
        self.assertEqual(problems, [])

    def test_empty_items_are_dropped(self):
        text = '{"events": [{"title": "空"}, {"title": "有", "text": "真的有事发生"}]}'
        events, problems = parse_generated_events(text)
        self.assertEqual([item["title"] for item in events], ["有"])
        self.assertTrue(any("没有事件内容" in item for item in problems))


class ClampTests(unittest.TestCase):
    def test_counts_are_clamped(self):
        self.assertEqual(clamp_zone_count(99), 4)
        self.assertEqual(clamp_zone_count(0), 1)
        self.assertEqual(clamp_map_zone_count(99), 6)
        self.assertEqual(clamp_map_zone_count(0), 4)
        self.assertEqual(clamp_map_nodes_per_zone(99), 5)
        self.assertEqual(clamp_map_nodes_per_zone(0), 3)
        self.assertEqual(clamp_world_action_count(99), 24)
        self.assertEqual(clamp_world_action_count(0), 8)
        self.assertEqual(clamp_schedule_count(99), 6)
        self.assertEqual(clamp_event_count("bad"), 3)


class WorldMapParserTests(unittest.TestCase):
    def test_zone_pack_reads_the_requested_nodes_per_zone(self):
        text = (
            '{"zones": [{"id": "home", "name": "家中", "nodes": ['
            '{"id": "bedroom", "name": "卧室"},'
            '{"id": "kitchen", "name": "厨房"},'
            '{"id": "balcony", "name": "阳台"}]}]}'
        )
        pack = parse_generated_zone_pack(text, max_nodes_per_zone=3)
        self.assertEqual(len(pack["zones"]), 1)
        self.assertEqual(
            [node["id"] for node in pack["nodes"]],
            ["bedroom", "kitchen", "balcony"],
        )
        self.assertTrue(all(node["zone_id"] == "home" for node in pack["nodes"]))
        self.assertEqual(pack["problems"], [])

    def test_reads_zones_nodes_and_connections(self):
        text = (
            '{"zones": ['
            '{"id": "old_town", "name": "老城区", "note": "她从小住的地方",'
            ' "nodes": [{"id": "square", "name": "广场", "prompt": "很热闹"},'
            ' {"id": "alley", "name": "小巷", "prompt": "安静"}]},'
            '{"id": "river_bank", "name": "河岸", "note": "适合发呆",'
            ' "nodes": [{"id": "dock", "name": "码头", "prompt": "能看船"}]}'
            "],"
            '"connections": [{"from_zone": "old_town", "from_node": "square",'
            ' "to_zone": "river_bank", "to_node": "dock", "ticks": 2}]}'
        )
        draft = parse_generated_world_map(text, max_zones=4)
        self.assertEqual([zone["id"] for zone in draft["zones"]], ["old_town", "river_bank"])
        self.assertEqual(len(draft["nodes"]), 3)
        self.assertEqual(draft["nodes"][0]["zone_id"], "old_town")
        self.assertEqual(len(draft["connections"]), 1)
        self.assertEqual(draft["connections"][0]["ticks"], 2)
        self.assertEqual(draft["problems"], [])

    def test_missing_nodes_get_a_starter_and_bad_links_are_dropped(self):
        text = (
            '{"zones": [{"id": "home", "name": "家中", "nodes": []},'
            ' {"id": "park", "name": "公园"}],'
            '"connections": [{"from_zone": "home", "from_node": "nowhere",'
            ' "to_zone": "park", "to_node": "missing"}]}'
        )
        draft = parse_generated_world_map(text)
        self.assertEqual(len(draft["nodes"]), 2)
        self.assertTrue(all(node["id"] for node in draft["nodes"]))
        self.assertEqual(draft["connections"], [])
        self.assertTrue(any("落脚点" in item for item in draft["problems"]))
        self.assertTrue(any("不存在的地点" in item for item in draft["problems"]))

    def test_connect_zone_graph_bridges_isolated_zones(self):
        zones = [
            {"id": "home", "x": 0, "y": 0},
            {"id": "town", "x": 200, "y": 0},
            {"id": "beach", "x": 400, "y": 0},
        ]
        nodes_by_zone = {
            "home": [{"id": "home_center", "x": 0, "y": 0}],
            "town": [{"id": "town_center", "x": 0, "y": 0}],
            "beach": [{"id": "beach_center", "x": 0, "y": 0}],
        }
        added = connect_zone_graph(
            zones,
            nodes_by_zone,
            existing_edges=[
                {"from_zone": "home", "to_zone": "town"},
            ],
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(
            {added[0]["from_zone"], added[0]["to_zone"]},
            {"town", "beach"},
        )
        self.assertTrue(added[0]["from_node"])
        self.assertTrue(added[0]["to_node"])

    def test_connect_zone_graph_walks_around_the_ring_instead_of_a_star(self):
        zones = [
            {"id": f"z{index}", "x": x, "y": y}
            for index, (x, y) in enumerate(auto_layout_ring(5))
        ]
        nodes_by_zone = {
            f"z{index}": [{"id": f"n{index}", "x": 0, "y": 0}]
            for index in range(5)
        }
        added = connect_zone_graph(zones, nodes_by_zone)
        self.assertEqual(len(added), 4)
        for edge in added:
            left = int(edge["from_zone"][1:])
            right = int(edge["to_zone"][1:])
            self.assertIn(abs(left - right), (1, 4))

    def test_zone_links_prefer_portal_nodes(self):
        """自动连线优先选入口 / 门口，不要拿厨房去连公园长椅。"""

        zones = [
            {"id": "home", "x": 0, "y": 0},
            {"id": "park", "x": 300, "y": 0},
        ]
        nodes_by_zone = {
            "home": [
                {"id": "home_kitchen", "name": "厨房", "x": 0, "y": 0},
                {"id": "home_gate", "name": "公寓玄关", "x": 0, "y": 200},
            ],
            "park": [
                {"id": "park_bench", "name": "公园长椅", "x": 0, "y": 0},
                {"id": "park_gate", "name": "公园大门", "x": 0, "y": 200},
            ],
        }
        added = connect_zone_graph(zones, nodes_by_zone)
        self.assertEqual(len(added), 1)
        self.assertEqual(
            {added[0]["from_node"], added[0]["to_node"]},
            {"home_gate", "park_gate"},
        )


class LayoutTests(unittest.TestCase):
    def test_ring_layout_keeps_every_zone_inside_positive_canvas(self):
        positions = auto_layout_ring(6)
        self.assertEqual(len(positions), 6)
        self.assertTrue(all(x >= 0 and y >= 0 for x, y in positions))
        xs = [x for x, _y in positions]
        ys = [y for _x, y in positions]
        self.assertGreater(max(xs) - min(xs), 300)
        self.assertGreater(max(ys) - min(ys), 300)

    def test_spring_layout_keeps_routed_zones_closer_than_unrouted_ones(self):
        zones = [{"id": f"z{index}"} for index in range(5)]
        edges = [
            {"from_zone": f"z{index}", "to_zone": f"z{index + 1}"}
            for index in range(4)
        ]
        positions = spring_layout_zones(zones, edges)
        self.assertEqual(len(positions), 5)
        self.assertTrue(all(x >= 0 and y >= 0 for x, y in positions.values()))

        def distance(left: str, right: str) -> float:
            lx, ly = positions[left]
            rx, ry = positions[right]
            return ((lx - rx) ** 2 + (ly - ry) ** 2) ** 0.5

        self.assertLess(distance("z0", "z1"), distance("z0", "z4"))
        self.assertLess(distance("z3", "z4"), distance("z0", "z4"))


class GeneratorPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        world, _warnings = parse_world(default_world())
        cls.builder = PromptBuilder(world)

    def test_zone_prompt_carries_persona_existing_world_and_relations(self):
        system, prompt = self.builder.build_zone_generator_prompt(
            persona_text="她是一个住在海边小镇的女孩。",
            existing_zones=[
                {"id": "home", "name": "家中", "note": "她住的地方"}
            ],
            existing_nodes=[{"id": "bedroom", "name": "卧室", "zone_id": "home"}],
            zone_edges=[{"from": "home", "to": "town"}],
            relations="她已经认识的人：\n- 小明（朋友）：常来串门",
            count=2,
            extra_prompt="爱丽丝的梦游仙境，偏童话一点",
        )
        self.assertIn("新的区域", system)
        self.assertIn("JSON", system)
        self.assertIn("门户", system)
        self.assertIn("海边小镇", prompt)
        self.assertIn("家中", prompt)
        self.assertIn("卧室", prompt)
        self.assertIn("小明", prompt)
        self.assertIn("home ↔ town", prompt)
        self.assertIn("爱丽丝的梦游仙境", prompt)

    def test_schedule_prompt_lists_actions_and_existing_schedules(self):
        _system, prompt = self.builder.build_schedule_generator_prompt(
            persona_text="她是一个很宅的人。",
            zones=[{"id": "home", "name": "家中", "note": ""}],
            nodes=[{"id": "kitchen", "name": "厨房", "zone": "home"}],
            actions=[{"id": "cook", "name": "做饭", "desc": "给自己做点吃的"}],
            existing_schedules=[{"id": "morning", "time": "08:00", "note": "起床"}],
            relations="",
            count=3,
        )
        self.assertIn("cook", prompt)
        self.assertIn("厨房", prompt)
        self.assertIn("morning", prompt)

    def test_event_prompt_lists_recent_events(self):
        _system, prompt = self.builder.build_event_generator_prompt(
            persona_text="她最近刚搬家。",
            zones=[{"id": "home", "name": "家中", "note": ""}],
            nodes=[{"id": "lobby", "name": "客厅"}],
            recent_events=["她在客厅里收拾箱子"],
            relations="",
            count=2,
        )
        self.assertIn("刚搬家", prompt)
        self.assertIn("收拾箱子", prompt)

    def test_world_map_prompt_asks_for_full_connected_map(self):
        system, prompt = self.builder.build_world_map_generator_prompt(
            persona_text="她住在海边小镇，喜欢在天台看星星。",
            relations="她已经认识的人：\n- 小明（朋友）：常来串门",
            count=4,
            nodes_per_zone=3,
            extra_prompt="爱丽丝的梦游仙境",
        )
        self.assertIn("整张重建", system)
        self.assertIn("连通", system)
        self.assertIn("门户", system)
        self.assertIn("厨房 ↔ 公园长椅", system)
        self.assertIn("海边小镇", prompt)
        self.assertIn("小明", prompt)
        self.assertIn("爱丽丝的梦游仙境", prompt)
        self.assertIn('"connections"', system)

    def test_autonomous_scene_hides_legacy_tools_until_migration_is_enabled(self):
        world, _warnings = parse_world(default_world())
        scene = PromptBuilder(world).scene_layer(
            world.node_map()["study"], {"web_search": "搜索网页"}
        )
        self.assertNotIn("通用工具", scene)
        self.assertNotIn("工具型动作", scene)

        legacy = default_world()
        legacy["legacy_tool_actions"] = True
        legacy["global_allowed_tools"] = ["web_search"]
        legacy_world, _warnings = parse_world(legacy)
        legacy_scene = PromptBuilder(legacy_world).scene_layer(
            legacy_world.node_map()["study"], {"web_search": "搜索网页"}
        )
        self.assertIn("通用工具", legacy_scene)

    def test_world_action_prompt_carries_persona_map_and_count(self):
        system, prompt = self.builder.build_world_action_generator_prompt(
            persona_text="她喜欢做饭，也喜欢在阳台养花。",
            zones=[{"id": "home", "name": "家中", "note": "她住的地方"}],
            nodes=[
                {
                    "id": "kitchen",
                    "name": "厨房",
                    "zone_id": "home",
                    "prompt": "有热茶和烤箱",
                }
            ],
            existing_actions=["发呆", "看风景"],
            count=6,
        )
        self.assertIn("JSON", system)
        self.assertIn("做饭", prompt)
        self.assertIn("厨房", prompt)
        self.assertIn("发呆", prompt)
        self.assertIn("6 个动作", prompt)


class EngineGeneratorTests(unittest.IsolatedAsyncioTestCase):
    """引擎层的端到端：假的内容生成模型 → 草稿解析 → 自动摆位 / 校验。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ConfigStore(self._tmp.name)
        self.store.ensure_files()
        self.store.load_world()
        self.store.add_session(
            SESSION, session_type="group", platform="aiocqhttp", cold_start_node="study"
        )
        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.creator = StubLLM()
        self.engine = MyWorldEngine(
            store=self.store,
            db=self.db,
            llm=StubLLM(['{"actions":[{"type":"say","messages":["嗯"]}]}']),
            creator_llm=self.creator,
            messenger=StubMessenger(),
            persona=StubPersona("你是一个住在老城区的女孩。"),
            clock=StubClock(now=1_700_000_000.0),
            tick_seconds=60.0,
        )
        self.engine.reload_config()

    async def test_generate_zones_returns_drafts_with_auto_positions(self):
        self.creator.replies = [
            '{"zones": [{"id": "old_library", "name": "旧图书馆", "note": "她小时候常来", '
            '"icon": "📚", "color": "#8B7DD8", "is_home": false}]}'
        ]
        result = await self.engine.generate_zones(1)
        zones = result["zones"]
        self.assertEqual([zone["id"] for zone in zones], ["old_library"])
        self.assertNotEqual((zones[0]["x"], zones[0]["y"]), (40.0, 60.0))
        # 模型没给地点时会自动补一个落脚点，这是提醒不是失败。
        self.assertTrue(all("落脚点" in item for item in result["problems"]))

    async def test_generate_zones_can_ask_for_several_nodes_per_zone(self):
        self.creator.replies = [
            '{"zones": [{"id": "rooftop", "name": "天台", "note": "看星星的地方", "nodes": ['
            '{"id": "rooftop_edge", "name": "天台边缘", "prompt": "风很大"},'
            '{"id": "water_tank", "name": "水箱旁", "prompt": "很安静"},'
            '{"id": "old_chair", "name": "旧椅子", "prompt": "能坐一会儿"}]}]}'
        ]
        result = await self.engine.generate_zones(1, 3)
        self.assertEqual(len(result["zones"]), 1)
        self.assertEqual(
            [node["id"] for node in result["nodes"]],
            ["rooftop_edge", "water_tank", "old_chair"],
        )
        self.assertTrue(all(node["zone_id"] == "rooftop" for node in result["nodes"]))
        # 同一个区域里的地点已经自动摆开，不会全叠在 (60, 60)。
        self.assertGreater(len({(node["x"], node["y"]) for node in result["nodes"]}), 1)

    async def test_generate_zones_adds_starter_nodes_and_a_zone_link(self):
        self.creator.replies = [
            '{"zones": [{"id": "old_library", "name": "旧图书馆", "note": "她小时候常来"}]}'
        ]
        result = await self.engine.generate_zones(1)
        zone = result["zones"][0]
        self.assertEqual(len(result["nodes"]), 1)
        node = result["nodes"][0]
        self.assertEqual(node["zone_id"], zone["id"])
        self.assertTrue(node["id"].startswith("old_library"))
        # 默认世界里已经有区域和地点，新区域应该接到其中一个已有区域。
        self.assertEqual(len(result["zone_edges"]), 1)
        edge = result["zone_edges"][0]
        self.assertIn(zone["id"], (edge["from_zone"], edge["to_zone"]))
        self.assertTrue(edge["from_node"] and edge["to_node"])

    async def test_generate_world_map_returns_a_connected_replacement(self):
        self.creator.replies = [
            '{"zones": ['
            '{"id": "seaside_home", "name": "海边小屋", "note": "她住的地方", "is_home": true,'
            ' "nodes": [{"id": "deck", "name": "露台入口", "prompt": "能看海"},'
            ' {"id": "kitchen", "name": "厨房", "prompt": "有热茶"}]},'
            '{"id": "lighthouse", "name": "灯塔", "note": "她晚上散步会来",'
            ' "nodes": [{"id": "top", "name": "灯塔门口", "prompt": "能看很远"}]}'
            "],"
            '"connections": [{"from_zone": "seaside_home", "from_node": "deck",'
            ' "to_zone": "lighthouse", "to_node": "top", "ticks": 3}]}'
        ]
        result = await self.engine.generate_world_map(2)
        self.assertEqual(len(result["zones"]), 2)
        self.assertEqual(len(result["nodes"]), 3)
        self.assertTrue(result["zones"][0]["is_home"])
        self.assertEqual(len(result["zone_edges"]), 1)
        self.assertEqual(result["zone_edges"][0]["ticks"], 3)
        self.assertTrue(result["edges"])
        self.assertEqual(result["problems"], [])

    async def test_generate_world_map_keeps_crossing_non_neighbor_routes(self):
        self.creator.replies = [
            '{"zones": ['
            '{"id": "z0", "name": "A", "nodes": [{"id": "n0", "name": "a"}]},'
            '{"id": "z1", "name": "B", "nodes": [{"id": "n1", "name": "b"}]},'
            '{"id": "z2", "name": "C", "nodes": [{"id": "n2", "name": "c"}]},'
            '{"id": "z3", "name": "D", "nodes": [{"id": "n3", "name": "d"}]}'
            "],"
            '"connections": ['
            '{"from_zone": "z0", "from_node": "n0", "to_zone": "z2", "to_node": "n2", "ticks": 4},'
            '{"from_zone": "z1", "from_node": "n1", "to_zone": "z3", "to_node": "n3", "ticks": 4}'
            "]}"
        ]
        result = await self.engine.generate_world_map(4, 1)
        pairs = {
            frozenset((edge["from_zone"], edge["to_zone"]))
            for edge in result["zone_edges"]
        }
        self.assertIn(frozenset(("z0", "z2")), pairs)
        self.assertIn(frozenset(("z1", "z3")), pairs)

    async def test_generate_world_actions_reads_persona_and_the_whole_map(self):
        # 已停用的动作不该再出现在"不要重复"清单里，默认动作按需启用后尤其重要。
        disabled = self.engine.world.action_map()["sing"]
        disabled.enabled = False
        self.creator.replies = [
            '{"actions": ['
            '{"id": "water_flowers", "node_id": "kitchen", "name": "给花浇水",'
            ' "description": "给窗台上的花浇水", "llm_level": "single", "category": "instant"},'
            '{"id": "watch_street", "node_id": "lobby", "name": "看街景",'
            ' "description": "站在窗边看楼下", "llm_level": "single", "category": "continuous",'
            ' "duration_mode": "llm", "duration_min": 300, "duration_max": 1800}'
            "]}"
        ]
        result = await self.engine.generate_world_actions(2)
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(
            {item["node_id"] for item in result["actions"]},
            {"kitchen", "lobby"},
        )
        self.assertTrue(all(item["scope"] == "node" for item in result["actions"]))
        self.assertEqual(result["problems"], [])
        prompt = self.creator.calls[-1]["prompt"]
        self.assertNotIn(disabled.name or "sing", prompt)

    async def test_generate_schedules_drops_unknown_steps(self):
        self.creator.replies = [
            '{"schedules": [{"id": "evening", "time": "20:30", "days": ["mon"], '
            '"note": "晚上去书房待一会儿", "action_chain": ['
            '{"type": "walk_to", "target_node": "study"},'
            '{"type": "think", "content": "看会儿书"},'
            '{"type": "launch_rocket", "intent": "不该存在"}]}]}'
        ]
        result = await self.engine.generate_schedules(1)
        schedules = result["schedules"]
        self.assertEqual(len(schedules), 1)
        self.assertEqual(
            [step["type"] for step in schedules[0]["action_chain"]],
            ["walk_to", "think"],
        )
        self.assertTrue(any("不可用" in item for item in result["problems"]))

    async def test_generate_events_returns_deliverable_drafts(self):
        self.creator.replies = [
            '{"events": [{"title": "旧朋友来信", "text": "她收到一封很多年没联系的人寄来的信。"}]}'
        ]
        result = await self.engine.generate_events(1)
        events = result["events"]
        self.assertEqual(len(events), 1)
        self.assertIn("信", events[0]["text"])

    async def test_editor_generators_preserve_provider_failure_reason(self):
        self.creator.generate = AsyncMock(return_value=LLMReply(ok=False, error="HTTP 429: quota exceeded"))
        for method in (self.engine.generate_zones, self.engine.generate_schedules, self.engine.generate_events):
            with self.subTest(generator=method.__name__):
                with self.assertRaisesRegex(ValueError, "429: quota exceeded"):
                    await method(1)

    async def test_empty_reply_and_missing_model_have_actionable_errors(self):
        with self.assertRaisesRegex(ValueError, "空内容"):
            await self.engine.generate_zones(1)
        self.engine.creator_llm = None
        with self.assertRaisesRegex(ValueError, "没有可用的内容生成模型"):
            await self.engine.generate_zones(1)

    async def test_provider_exception_is_reported_but_background_calls_still_fall_back(self):
        self.creator.generate = AsyncMock(side_effect=RuntimeError("connection refused"))
        with self.assertRaisesRegex(ValueError, "connection refused"):
            await self.engine.generate_zones(1)
        self.assertIsNone(await self.engine._ask_creator(SESSION, "", ""))


if __name__ == "__main__":
    unittest.main()
