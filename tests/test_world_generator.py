"""世界生成（区域 / 日程 / 事件）：解析器与提示词的纯逻辑测试。

生成结果永远只是**草稿**：解析器负责挡住模型瞎编的 id、重复的 id、
不存在的动作与地点，再把干净的草稿交给编辑器让用户改。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.defaults import default_world  # noqa: E402
from core.generator import (  # noqa: E402
    clamp_event_count,
    clamp_schedule_count,
    clamp_zone_count,
    parse_generated_events,
    parse_generated_schedules,
    parse_generated_zones,
)
from core.models import parse_world  # noqa: E402
from core.prompt import PromptBuilder  # noqa: E402


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
        self.assertEqual(clamp_schedule_count(99), 6)
        self.assertEqual(clamp_event_count("bad"), 3)


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
        )
        self.assertIn("新的区域", system)
        self.assertIn("JSON", system)
        self.assertIn("海边小镇", prompt)
        self.assertIn("家中", prompt)
        self.assertIn("卧室", prompt)
        self.assertIn("小明", prompt)
        self.assertIn("home ↔ town", prompt)

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


if __name__ == "__main__":
    unittest.main()
