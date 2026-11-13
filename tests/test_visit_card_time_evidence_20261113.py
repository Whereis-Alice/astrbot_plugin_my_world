"""Exported keepsakes never turn unreached plans into shared experience."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace as NS

from core.visit_card import build_visit_card_pages
from core.visits.record_cards import VisitRecordCards
from tests.test_visit_card_layout import visit


def stops(page):
    return [stop for entry in page["entries"] if entry["category"] == "route" for stop in entry["stops"]]


def on_road():
    return visit(arrived_at=90, ended_at=160, scope="life", role="host", host_id="home:isla", guest_id="away:alice",
                 outing={"stage": 0, "place": "庭院", "activity": "喝茶", "started_at": 180, "until": 500,
                         "departed_at": 100, "travel_seconds": 80, "origin_place": "客厅",
                         "route": [{"place": "庭院", "activity": "喝茶", "started_at": 180, "until": 500,
                                    "departed_at": 100, "travel_seconds": 80, "origin_place": "客厅"}]})


def test_journey_ended_before_arrival_has_no_destination_activity_or_walked_route():
    source = on_road()
    original = copy.deepcopy(source)
    page = build_visit_card_pages(source)[0]
    assert page["title"] == "客厅 → 庭院"
    assert "尚未开始" in page["activity"] and "喝茶" not in page["activity"]
    assert not stops(page)
    assert source == original


def test_interrupted_unreached_stage_stays_out_of_experience_after_later_activity():
    source = on_road()
    source["ended_at"] = 300
    source["outing"]["route"][0]["ended_at"] = 160  # Changed destination during the original journey.
    next_stop = {"place": "小路", "activity": "散步", "started_at": 200, "until": 600}
    source["outing"]["route"].append(next_stop)
    source["outing"].update(next_stop, stage=1, travel_seconds=40, origin_place="客厅")
    page = build_visit_card_pages(source)[0]
    assert [row["place"] for row in stops(page)] == ["小路"]
    assert page["title"] == "小路" and page["activity"] == "散步"


def test_stop_actual_end_can_prove_no_arrival_even_when_visit_ends_later():
    source = on_road()
    source["ended_at"] = 300
    source["outing"]["route"][0]["ended_at"] = 160
    page = build_visit_card_pages(source)[0]
    assert not stops(page)
    assert page["title"] == "客厅 → 庭院" and "尚未开始" in page["activity"]


def test_public_export_preserves_journey_and_actual_departure_evidence_without_private_data():
    source = on_road()
    source["outing"]["ended_at"] = 160
    source["outing"]["route"][0].update(ended_at=160, private="PRIVATE-STOP")
    source["outing"]["private"] = "PRIVATE-OUTING"
    source["local"]["persona_key"] = "PRIVATE-PERSONA"
    source["log"] = [{"seq": 1, "actor_id": "home:isla", "kind": "say", "text": "回去吧。", "thought": "PRIVATE-THOUGHT"}]
    original = copy.deepcopy(source)
    snapshot = VisitRecordCards._record_card_snapshot(NS(_state={"instance_id": "home"}), source)
    for row in (snapshot["outing"], snapshot["outing"]["route"][0]):
        assert row["travel_seconds"] == 80 and row["origin_place"] == "客厅"
        assert row["ended_at"] == 160 and row["departed_at"] == 100
    assert "PRIVATE-" not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["log"][0] == {"seq": 1, "actor_id": "home:isla", "kind": "say", "text": "回去吧。"}
    assert "尚未开始" in build_visit_card_pages(snapshot)[0]["activity"]
    snapshot["outing"]["route"][0]["origin_place"] = "修改预览"
    assert source == original


def test_old_routes_without_clocks_remain_complete_without_claiming_an_outcome():
    source = visit(outing={"route": [{"place": "茶室", "activity": "喝茶"}, {"location": "林间", "title": "散步"}]})
    page = build_visit_card_pages(source)[0]
    assert [row["place"] for row in stops(page)] == ["茶室", "林间"]
    assert all("完成" not in row["activity"] for row in stops(page))


def test_full_travel_plan_log_keeps_its_source_and_intention_category():
    source = on_road()
    source["log"] = [{"seq": 1, "actor_id": "away:alice", "kind": "travel_plan", "text": "前往庭院，抵达后计划喝茶"}]
    page = build_visit_card_pages(source)[0]
    entry = next(row for row in page["entries"] if row.get("kind") == "travel_plan")
    assert entry["side"] == "remote" and entry["speaker"] == "爱丽丝" and entry["seq"] == 1
    assert entry["parts"] == [{"type": "intent", "label": "当时的打算", "text": source["log"][0]["text"]}]


def test_public_milestones_and_action_adjustments_keep_text_sources_and_evidence_labels():
    source = on_road()
    source["log"] = [
        {"seq": 1, "actor_id": "home:isla", "kind": "milestone", "text": "已经把茶杯摆在桌上。"},
        {"seq": 2, "actor_id": "away:alice", "kind": "action_control", "text": "取消继续赶路，先在原处等一会儿。"},
    ]
    snapshot = VisitRecordCards._record_card_snapshot(NS(_state={"instance_id": "home"}), source)
    assert snapshot["log"] == source["log"]
    rows = build_visit_card_pages(snapshot)[0]["entries"]
    assert [row["category"] for row in rows] == ["result", "chronicle"]
    assert [row["side"] for row in rows] == ["local", "remote"]
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[1]["tag"] == "行动调整"
    assert [row["parts"][0]["text"] for row in rows] == [row["text"] for row in source["log"]]


def test_unaccepted_scene_after_meeting_is_not_an_exported_shared_activity():
    source = visit(outing_version=1, outing={}, scene={"place": "游乐园", "activity": "坐过山车"})
    page = build_visit_card_pages(source)[0]
    assert page["title"] == "一次相见，留在这里" and not page["activity"]
