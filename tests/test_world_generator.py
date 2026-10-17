"""世界生成（区域 / 日程 / 事件）：解析器与提示词的纯逻辑测试。

生成结果永远只是**草稿**：解析器负责挡住模型瞎编的 id、重复的 id、
不存在的动作与地点，再把干净的草稿交给编辑器让用户改。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.engine import MyWorldEngine  # noqa: E402
from core.generator import (  # noqa: E402
    ASPECT_MAX,
    ASPECT_MIN,
    DAY_KEYS,
    auto_layout_ring,
    connect_zone_graph,
    clamp_event_count,
    clamp_map_nodes_per_zone,
    clamp_map_zone_count,
    clamp_schedule_count,
    clamp_world_action_count,
    clamp_zone_count,
    grid_layout_nodes,
    layout_frame,
    normalize_aspect,
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

    def test_one_off_schedule_keeps_its_date_and_clears_the_weekdays(self):
        text = (
            '{"schedules": [{"id": "buy_fish", "time": "17:30", "once": true, '
            '"date": "2026-10-18", "note": "答应给她买鱼", "action_chain": [{"type": "say"}]}]}'
        )
        schedules, problems = parse_generated_schedules(
            text, action_ids={"say"}, node_ids=set()
        )
        self.assertEqual(len(schedules), 1)
        schedule = schedules[0]
        self.assertTrue(schedule["once"])
        self.assertEqual(schedule["date"], "2026-10-18")
        self.assertEqual(schedule["days"], [])
        self.assertEqual(problems, [])

    def test_one_off_schedule_with_a_broken_date_falls_back_to_daily(self):
        text = (
            '{"schedules": [{"id": "buy_fish", "time": "17:30", "once": true, '
            '"date": "下周三", "action_chain": [{"type": "say"}]}]}'
        )
        schedules, problems = parse_generated_schedules(text, action_ids={"say"})
        self.assertEqual(len(schedules), 1)
        self.assertFalse(schedules[0]["once"])
        self.assertEqual(schedules[0]["date"], "")
        self.assertEqual(schedules[0]["days"], list(DAY_KEYS))
        self.assertTrue(any("日期" in item for item in problems))


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

    def test_event_flavor_and_place_are_kept_for_the_editor(self):
        text = (
            '{"events": ['
            '{"title": "门口的小猫", "text": "楼下保安在门口捡到一只小猫。", '
            '"kind": "good", "place": "公寓玄关"},'
            '{"title": "水管爆了", "text": "厨房水管突然漏水。", "kind": "BAD"},'
            '{"title": "怪事", "text": "她发现窗台上多了一颗不认识的石子。", "kind": "玄学"}'
            "]}"
        )
        events, _problems = parse_generated_events(text, max_events=4)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["kind"], "good")
        self.assertEqual(events[0]["place"], "公寓玄关")
        # 大小写照收，认不出的味道不写（别把自由文本当成枚举塞给前端）
        self.assertEqual(events[1]["kind"], "bad")
        self.assertNotIn("kind", events[2])


class ClampTests(unittest.TestCase):
    def test_counts_are_clamped(self):
        self.assertEqual(clamp_zone_count(99), 4)
        self.assertEqual(clamp_zone_count(0), 1)
        self.assertEqual(clamp_map_zone_count(99), 6)
        self.assertEqual(clamp_map_zone_count(0), 4)
        self.assertEqual(clamp_map_nodes_per_zone(99), 8)
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

    def test_aspect_ratio_is_clamped_and_garbage_falls_back_to_square(self):
        self.assertEqual(normalize_aspect("abc"), 1.0)
        self.assertEqual(normalize_aspect(0), 1.0)
        self.assertEqual(normalize_aspect(None), 1.0)
        self.assertEqual(normalize_aspect(99), ASPECT_MAX)
        self.assertEqual(normalize_aspect(0.01), ASPECT_MIN)
        self.assertAlmostEqual(normalize_aspect("1.5"), 1.5)

    def test_layout_frame_keeps_the_canvas_shape(self):
        for aspect in (0.6, 1.0, 2.0):
            width, height = layout_frame(6, aspect)
            self.assertAlmostEqual(width / height, aspect, delta=1e-6)
        # 区域越多，画框越大；不然十来个区域会挤成一团。
        self.assertLess(layout_frame(2, 1.0)[0], layout_frame(9, 1.0)[0])

    def test_spring_layout_fills_the_shape_it_is_asked_for(self):
        # 生成地图时前端会把画布形状传进来：算出来的图要跟窗口一个形状，
        # 否则一张扁图放进高窗口，永远只在上面占一小块。
        zones = [{"id": f"z{index}", "is_home": index == 0} for index in range(6)]
        edges = [
            {"from_zone": "z0", "to_zone": "z1"},
            {"from_zone": "z1", "to_zone": "z2"},
            {"from_zone": "z2", "to_zone": "z3"},
            {"from_zone": "z3", "to_zone": "z4"},
        ]
        for aspect in (0.7, 1.0, 2.4):
            positions = spring_layout_zones(zones, edges, aspect=aspect)
            xs = [x for x, _y in positions.values()]
            ys = [y for _x, y in positions.values()]
            box_w = max(xs) - min(xs)
            box_h = max(ys) - min(ys)
            self.assertGreater(box_w, 0)
            self.assertGreater(box_h, 0)
            self.assertAlmostEqual(box_w / box_h, aspect, delta=0.05)

    def test_seeded_layout_keeps_hand_placed_zones_on_their_side(self):
        zones = [{"id": "home"}, {"id": "street"}, {"id": "cafe"}, {"id": "park"}]
        edges = [
            {"from_zone": "home", "to_zone": "cafe"},
            {"from_zone": "street", "to_zone": "park"},
            {"from_zone": "home", "to_zone": "street"},
        ]
        positions = spring_layout_zones(
            zones,
            edges,
            aspect=1.0,
            seed={"home": (80.0, 80.0), "street": (520.0, 90.0)},
        )
        # 增量生成新区域时，既有区域不能整个翻掉：左右关系保持原样。
        self.assertLess(positions["home"][0], positions["street"][0])
        self.assertEqual(set(positions), {"home", "street", "cafe", "park"})

    def test_grid_layout_follows_the_canvas_shape(self):
        wide = grid_layout_nodes(8, aspect=2.2)
        tall = grid_layout_nodes(8, aspect=0.6)
        self.assertEqual(len(wide), 8)
        self.assertEqual(len(tall), 8)
        # 宽画布排成一排更多列、行更少；高画布反过来。
        self.assertGreater(max(x for x, _y in wide), max(x for x, _y in tall))
        self.assertLess(max(y for _x, y in wide), max(y for _x, y in tall))
        self.assertEqual(grid_layout_nodes(0), [])
        self.assertEqual(grid_layout_nodes("nonsense"), [])


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

    def test_generic_action_prompt_drops_persona_and_can_drop_the_map(self):
        builder = PromptBuilder(parse_world(default_world())[0])
        zones = [{"id": "home", "name": "家里", "note": "她住的地方"}]
        nodes = [{"id": "kitchen", "name": "厨房", "zone_id": "home"}]
        _system, free = builder.build_generic_action_generator_prompt(
            zones=zones, nodes=nodes, existing_actions=[], count=6, use_map=False
        )
        # 不看地图时连地点名都不给模型看，它写的是与地点无关的通用小事
        self.assertNotIn("kitchen", free)
        self.assertNotIn("厨房", free)
        self.assertIn("通用动作", free)
        _system, with_map = builder.build_generic_action_generator_prompt(
            zones=zones, nodes=nodes, existing_actions=[], count=6, use_map=True
        )
        self.assertIn("kitchen", with_map)
        self.assertIn("厨房", with_map)

    def test_event_prompt_asks_for_good_bad_and_weird(self):
        builder = PromptBuilder(parse_world(default_world())[0])
        system, prompt = builder.build_event_generator_prompt(
            persona_text="她住在海边小镇。", count=4
        )
        self.assertIn("好事", system)
        self.assertIn("糟心事", system)
        self.assertIn("怪", system)
        self.assertIn("她住在海边小镇", prompt)
        weird_system, _prompt = builder.build_event_generator_prompt(
            persona_text="她住在海边小镇。", count=2, flavor="weird"
        )
        self.assertIn("反常", weird_system)

    def test_one_off_schedule_prompt_carries_today_and_extra_prompt(self):
        builder = PromptBuilder(parse_world(default_world())[0])
        system, prompt = builder.build_schedule_generator_prompt(
            persona_text="她住在海边小镇。",
            count=2,
            flavor="once",
            today="2026-10-16",
            extra_prompt="这周末要去参加朋友的婚礼",
        )
        self.assertIn("一次性", system)
        self.assertIn("2026-10-16", prompt)
        self.assertIn("婚礼", prompt)

    def test_profile_block_can_skip_facts_imported_from_the_weaver(self):
        """心迹画像自己也在注入时，这边跳过"从它那儿搬来的"那些事实，不重复说一遍。"""

        builder = PromptBuilder(parse_world(default_world())[0])
        person = {
            "name": "小明",
            "user_id": "42",
            "facts": [
                {"kind": "profile", "text": "爱好：摄影", "evidence": "导入自心迹画像"},
                {"kind": "profile", "text": "职业：学生", "evidence": "他说的"},
            ],
        }
        self.assertIn("爱好：摄影", builder.profile_block(person))
        skipped = builder.profile_block(person, skip_weaver_facts=True)
        self.assertNotIn("爱好：摄影", skipped)
        self.assertIn("职业：学生", skipped)

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

    async def test_world_map_can_draft_place_actions_in_one_go(self):
        """一键重建可以顺带给每个新地点起草地点专属动作（多花一次内容生成调用）。"""

        self.creator.replies = [
            '{"zones": [{"id": "seaside_home", "name": "海边小屋", "note": "她住的地方",'
            ' "is_home": true, "nodes": [{"id": "deck", "name": "露台", "prompt": "能看海"}]}]}',
            '{"actions": [{"id": "deck_watch_sea", "node_id": "deck", "name": "看海",'
            ' "description": "趴在栏杆上看海", "category": "continuous",'
            ' "llm_level": "single", "visible": true, "duration_mode": "llm",'
            ' "duration_min": 300, "duration_max": 900}]}',
        ]
        result = await self.engine.generate_world_map(2, 2, "", None, 2)

        self.assertEqual([node["id"] for node in result["nodes"]], ["deck"])
        self.assertEqual([item["id"] for item in result["actions"]], ["deck_watch_sea"])
        # 生成的动作一律先绑到它自己的那个地点，用户在动作库里再改
        self.assertEqual(result["actions"][0]["scope"], "node")
        self.assertEqual(result["actions"][0]["allowed_nodes"], ["deck"])

    async def test_actions_are_skipped_when_the_count_is_zero(self):
        """「每个地点生成几个动作」填 0 就不该多花那次调用。"""

        self.creator.replies = [
            '{"zones": [{"id": "seaside_home", "name": "海边小屋", "is_home": true,'
            ' "nodes": [{"id": "deck", "name": "露台", "prompt": "能看海"}]}]}'
        ]
        result = await self.engine.generate_world_map(2, 1, "", None, 0)

        self.assertEqual(result["actions"], [])
        self.assertEqual(len(self.creator.calls), 1)

    async def test_generate_zone_draft_can_replace_one_zone(self):
        """单个区域重做：给新地点 + 可选动作，replace 时列出要清掉的旧地点。"""

        raw = self.store.raw_world()
        zone_id = str((raw.get("zones") or [{}])[0].get("id") or "")
        self.assertTrue(zone_id)
        before = [
            node["id"] for node in (raw.get("nodes") or []) if node.get("zone_id") == zone_id
        ]
        self.assertTrue(before)
        self.creator.replies = [
            '{"nodes": [{"id": "brand_new_spot", "name": "新角落", "prompt": "刚收拾出来"}]}',
            '{"actions": [{"id": "new_spot_sit", "node_id": "brand_new_spot", "name": "坐一会儿",'
            ' "description": "在这儿坐一会儿", "category": "instant", "llm_level": "single"}]}',
        ]
        result = await self.engine.generate_zone_draft(
            zone_id, 1, "多来点安静的角落", 1, True
        )

        self.assertEqual(result["zone"], zone_id)
        self.assertEqual([node["id"] for node in result["nodes"]], ["brand_new_spot"])
        self.assertTrue(all(node["zone_id"] == zone_id for node in result["nodes"]))
        self.assertEqual([item["id"] for item in result["actions"]], ["new_spot_sit"])
        # replace=True：原来的地点列出来，交给编辑器确认后再删
        self.assertEqual(sorted(result["remove_nodes"]), sorted(before))
        # 额外提示词确实进了提示词里
        self.assertIn("多来点安静的角落", self.creator.calls[-1]["prompt"])

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

    async def test_generate_world_map_shapes_the_map_like_the_canvas(self):
        # 前端会把画布宽高比传进来，草稿的坐标要铺成同样的形状。
        self.creator.replies = [
            '{"zones": ['
            '{"id": "z0", "name": "A", "is_home": true, "nodes": [{"id": "n0", "name": "a"}]},'
            '{"id": "z1", "name": "B", "nodes": [{"id": "n1", "name": "b"}]},'
            '{"id": "z2", "name": "C", "nodes": [{"id": "n2", "name": "c"}]},'
            '{"id": "z3", "name": "D", "nodes": [{"id": "n3", "name": "d"}]}'
            "],"
            '"connections": ['
            '{"from_zone": "z0", "from_node": "n0", "to_zone": "z1", "to_node": "n1", "ticks": 2},'
            '{"from_zone": "z1", "from_node": "n1", "to_zone": "z2", "to_node": "n2", "ticks": 2},'
            '{"from_zone": "z2", "from_node": "n2", "to_zone": "z3", "to_node": "n3", "ticks": 2}'
            "]}"
        ]
        result = await self.engine.generate_world_map(4, 1, "", 2.2)
        xs = [zone["x"] for zone in result["zones"]]
        ys = [zone["y"] for zone in result["zones"]]
        self.assertAlmostEqual((max(xs) - min(xs)) / (max(ys) - min(ys)), 2.2, delta=0.1)

    async def test_generate_zones_returns_positions_for_the_whole_map(self):
        self.creator.replies = [
            '{"zones": [{"id": "old_library", "name": "旧图书馆", "note": "她小时候常来"}]}'
        ]
        result = await self.engine.generate_zones(1, 1, "", 1.4)
        positions = result["positions"]
        # 既有区域也要给回新坐标，前端才能一次性把整张图铺开。
        self.assertEqual(
            set(positions), {zone.id for zone in self.engine.world.zones} | {"old_library"}
        )
        self.assertEqual(result["zones"][0]["x"], positions["old_library"][0])
        xs = [point[0] for point in positions.values()]
        ys = [point[1] for point in positions.values()]
        self.assertAlmostEqual((max(xs) - min(xs)) / (max(ys) - min(ys)), 1.4, delta=0.1)

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

    async def test_generate_generic_actions_spreads_them_when_the_map_is_ignored(self):
        self.creator.replies = [
            '{"actions": ['
            '{"id": "brew_tea", "name": "泡一壶茶", "description": "慢慢泡一壶茶"},'
            '{"id": "fold_clothes", "name": "叠衣服", "description": "把晾干的衣服叠好"}'
            "]}"
        ]
        result = await self.engine.generate_generic_actions(2, False)
        actions = result["actions"]
        self.assertEqual(len(actions), 2)
        known = {node.id for node in self.engine.world.nodes}
        # 不看地图生成的动作也要落在真地点上，而且不会全堆在同一个地点
        self.assertTrue(all(action["node_id"] in known for action in actions))
        self.assertEqual(len({action["node_id"] for action in actions}), 2)
        self.assertTrue(all(action["scope"] == "node" for action in actions))
        prompt = self.creator.calls[-1]["prompt"]
        # 通用动作用的提示词里不该出现人设
        self.assertNotIn("你是一个住在老城区的女孩", prompt)

    async def test_generate_schedules_can_ask_for_one_off_plans(self):
        self.creator.replies = [
            '{"schedules": [{"id": "buy_fish", "time": "17:30", "once": true, '
            '"date": "2026-10-18", "note": "答应给她买鱼", '
            '"action_chain": [{"type": "think", "content": "出门买鱼"}]}]}'
        ]
        result = await self.engine.generate_schedules(1, "once", "这周末要请客")
        schedules = result["schedules"]
        self.assertEqual(len(schedules), 1)
        self.assertTrue(schedules[0]["once"])
        self.assertEqual(schedules[0]["date"], "2026-10-18")
        call = self.creator.calls[-1]
        self.assertIn("一次性", call["system_prompt"])
        self.assertIn("这周末要请客", call["prompt"])

    async def test_generate_events_passes_the_flavor_to_the_prompt(self):
        self.creator.replies = [
            '{"events": [{"title": "怪事", "text": "窗台上多了一颗石子。", "kind": "weird"}]}'
        ]
        result = await self.engine.generate_events(1, "weird", "多写点做梦的事")
        self.assertEqual(result["events"][0]["kind"], "weird")
        call = self.creator.calls[-1]
        self.assertIn("反常", call["system_prompt"])
        self.assertIn("多写点做梦的事", call["prompt"])

    async def test_auto_schedule_generation_runs_once_per_day(self):
        """开了「自动生成」之后，每天到点自己排几条一次性日程，同一天不重复排。"""

        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "per_day": 1, "hour": 0, "extra_prompt": "最近在准备考试"}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        today = self.engine._today_key(self.engine._now())
        self.creator.replies = [
            '{"schedules": [{"id": "buy_fish", "time": "17:30", "once": true, '
            f'"date": "{today}", "note": "出门买点东西", '
            '"action_chain": [{"type": "think", "content": "出门买东西"}]}]}'
        ]
        note = await self.engine.maybe_auto_generate_schedules()
        self.assertIn("1 条", note)
        rows = self.engine.schedules.schedules
        self.assertTrue(any(item.once and item.id == "buy_fish" for item in rows))
        prompt = self.creator.calls[-1]["prompt"]
        self.assertIn("最近在准备考试", prompt)

        # 同一天再跑一次：不再生成，也不再多调一次模型
        calls = len(self.creator.calls)
        self.assertEqual(await self.engine.maybe_auto_generate_schedules(), "")
        self.assertEqual(len(self.creator.calls), calls)

    async def test_stale_one_off_schedules_are_cleaned_by_the_same_job(self):
        """过期没跑成的一次性安排留着也没用：自动排的时候顺手清掉。"""

        raw = self.store.raw_schedules()
        raw["schedules"] = [
            {
                "id": "old_plan",
                "time": "09:00",
                "once": True,
                "date": "2020-01-01",
                "days": [],
                "action_chain": [{"type": "think", "content": "早就过去了"}],
            }
        ]
        raw["auto"] = {"enabled": True, "per_day": 1, "hour": 0}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.creator.replies = ['{"schedules": [{"id": "noop", "time": "10:00", "once": true, "date": "' + self.engine._today_key(self.engine._now()) + '", "action_chain": [{"type": "think", "content": "今天的事"}]}]}']
        await self.engine.maybe_auto_generate_schedules()
        ids = [item.id for item in self.engine.schedules.schedules]
        self.assertNotIn("old_plan", ids)

    async def test_profile_weaver_sync_maps_records_to_their_session(self):
        """自动同步按记录里的会话分别落库，不认得的会话跳过。"""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / "astrbot_plugin_profile_weaver" / "profiles.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "profiles": {
                        f"{SESSION}_42": {
                            "subject_user_id": "42",
                            "fields": {"昵称": "小明", "爱好": "摄影"},
                        },
                        "unknown:GroupMessage:1_7": {
                            "subject_user_id": "7",
                            "fields": {"昵称": "没人认得的群"},
                        },
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = await self.engine.sync_profile_weaver_by_session(str(root))
        self.assertEqual(result["people"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertGreater(result["added"], 0)
        facts = self.engine.profiles.facts(SESSION, "42", limit=10)
        self.assertIn("爱好：摄影", [item["text"] for item in facts])

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
