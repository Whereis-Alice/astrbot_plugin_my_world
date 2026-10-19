"""世界生成：区域、日程与事件。"""

from __future__ import annotations

import time
import math
from datetime import timedelta
from typing import Any
from ..generator import (
    DEFAULT_MAP_NODES_PER_ZONE,
    clamp_event_count,
    clamp_map_nodes_per_zone,
    clamp_map_zone_count,
    clamp_schedule_count,
    clamp_zone_count,
    connect_zone_graph,
    grid_layout_nodes,
    link_plan,
    looks_like_portal,
    parse_generated_events,
    parse_generated_schedules,
    parse_generated_world_map,
    parse_generated_zone_pack,
    spring_layout_zones,
)
from ..json_actions import CANCEL_MODES
from ..planner import same_plan
from ..models import (
    ActionDef,
    action_is_legacy,
)
from ..state import (
    STATE_DROWSY,
    STATE_IDLE,
    WorldState,
)
from .models import (
    MessageContext,
    TickOutcome,
)
from .util import _clip_text


def _wants_node_actions(value: Any) -> bool:
    """「每个地点生成几个动作」：0 / 空 = 不生成（和 clamp_per_node 的最小值 1 区分开）。"""

    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


class GenerationMixin:
    # ---------------- 世界生成：区域 / 日程 / 事件 ----------------

    def legacy_actions(self) -> list[ActionDef]:
        """当前世界里的全部旧版（工具 / 指令型）动作。"""

        return [action for action in self.world.actions if action_is_legacy(action)]

    async def purge_legacy_actions(self, *, confirm: bool = False) -> dict[str, Any]:
        """清除全部旧版动作（连同它们留下的空分组一起清掉）。

        ``confirm=False`` 只做预览：返回会删哪些动作、涉及哪些分组，不动配置。
        真正删除时会把 id 记进 ``purged_actions``：以后即使打开旧版开关、或者按内置
        动作表补回缺失项，也不会把它们复活。
        """

        legacy = self.legacy_actions()
        groups: dict[str, int] = {}
        for action in legacy:
            name = str(action.group or "").strip()
            if name:
                groups[name] = groups.get(name, 0) + 1
        preview = {
            "count": len(legacy),
            "actions": [
                {
                    "id": action.id,
                    "name": action.name or action.id,
                    "group": str(action.group or ""),
                    "kind": str(action.kind or ""),
                    "llm_level": str(action.llm_level or ""),
                }
                for action in legacy
            ],
            "groups": sorted(groups),
        }
        if not legacy:
            return {
                "ok": True,
                "removed": 0,
                "preview": preview,
                "note": "没有旧版动作，不用清",
            }
        if not confirm:
            return {
                "ok": True,
                "removed": 0,
                "preview": preview,
                "note": f"将要清除 {len(legacy)} 个旧版动作；确认后再执行",
            }

        removed_ids = {action.id for action in legacy}
        raw = self.store.raw_world()
        raw_actions = [
            item
            for item in (raw.get("actions") or [])
            if not (
                isinstance(item, dict)
                and str(item.get("id") or "") in removed_ids
            )
        ]
        raw["actions"] = raw_actions
        purged = {
            str(item).strip()
            for item in (raw.get("purged_actions") or [])
            if str(item).strip()
        } | removed_ids
        raw["purged_actions"] = sorted(purged)
        warnings = self.store.save_world(raw, reason="清除旧版动作")
        self.reload_config()
        state_notes: list[str] = []
        try:
            state_notes = await self.repair_states_after_config_change()
        except Exception as exc:  # 修状态失败不影响"已经删掉"这个事实
            self._log("warning", f"清除旧版动作后修状态失败：{exc}")
        remaining_groups = {
            str(action.group or "").strip()
            for action in self.world.actions
            if str(action.group or "").strip()
        }
        empty_groups = sorted(name for name in groups if name not in remaining_groups)
        note = f"已清除 {len(removed_ids)} 个旧版动作"
        if empty_groups:
            note += f"，顺带清掉 {len(empty_groups)} 个空分组：" + "、".join(
                empty_groups[:6]
            ) + ("…" if len(empty_groups) > 6 else "")
        self._log("info", note)
        return {
            "ok": True,
            "removed": len(removed_ids),
            "removed_actions": preview["actions"],
            "groups": empty_groups,
            "state_notes": state_notes,
            "warnings": warnings,
            "note": note,
        }

    def relations_brief(self, session_id: str = "", *, limit: int = 12) -> str:
        """给生成器看的人际关系摘要：她世界里的关系类型 + 已经认识的人。"""

        head: list[str] = []
        try:
            bonds = [
                str(getattr(item, "name", "") or "").strip()
                for item in (self.world.profile.bonds or [])
            ]
            if bonds:
                head.append("允许的关系类型：" + "、".join(item for item in bonds if item))
            levels = [
                str(getattr(item, "name", "") or "").strip()
                for item in (self.world.profile.levels or [])
            ]
            if levels:
                head.append("关系分级：" + " → ".join(item for item in levels if item))
        except Exception:
            pass
        people: list[dict[str, Any]] = []
        if session_id:
            try:
                people = self.profiles.list_people(session_id, limit=limit)
            except Exception:
                people = []
        lines: list[str] = []
        for person in people:
            if not isinstance(person, dict):
                continue
            name = str(person.get("name") or "").strip()
            if not name:
                names = (person.get("payload") or {}).get("names") or []
                name = str(names[-1] if names else person.get("user_id") or "").strip()
            if not name:
                continue
            level = str(
                person.get("level_name")
                or person.get("level")
                or person.get("bond")
                or ""
            ).strip()
            digest = " ".join(str(person.get("digest") or "").split())
            line = f"- {name}"
            if level:
                line += f"（{level}）"
            if digest:
                line += f"：{digest[:60]}"
            lines.append(line)
        if lines:
            head.append("她已经认识的人：")
            head.extend(lines)
        return "\n".join(head)

    async def generate_zones(
        self,
        count: Any = 0,
        nodes_per_zone: Any = 0,
        extra_prompt: str = "",
        aspect: Any = None,
        actions_per_node: Any = 0,
    ) -> dict[str, Any]:
        """按她的人设与已有世界生成新区域草稿（不落库，先给用户改）。

        ``nodes_per_zone`` 控制每个新区域同时生成几个地点；默认 1 个落脚点。
        ``aspect`` 是编辑器画布的宽高比：整张图会按这个形状重新铺开，
        新区域顺着既有地图的轮廓长出去，而不是一律堆在右边。
        ``actions_per_node`` > 0 时顺带给每个新地点起草几条**地点专属动作**
        （会多花一次内容生成调用）。
        """

        wanted = clamp_zone_count(count)
        per_zone = clamp_map_nodes_per_zone(nodes_per_zone, default=1)
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_zone_generator_prompt(
            persona_text=await self._persona_text(session_id),
            existing_zones=[
                {
                    "id": zone.id,
                    "name": zone.name or zone.id,
                    "note": zone.note,
                }
                for zone in self.world.zones
            ],
            existing_nodes=[
                {
                    "id": node.id,
                    "name": node.name or node.id,
                    "zone_id": node.zone_id,
                }
                for node in self.world.nodes
            ],
            zone_edges=[
                {"from": edge.from_zone, "to": edge.to_zone}
                for edge in (getattr(self.world, "zone_edges", None) or [])
            ],
            relations=self.relations_brief(session_id),
            count=wanted,
            nodes_per_zone=per_zone,
            extra_prompt=extra_prompt,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt, raise_errors=True)
        pack = parse_generated_zone_pack(
            reply,
            existing_zone_ids={zone.id for zone in self.world.zones},
            existing_node_ids={node.id for node in self.world.nodes},
            max_zones=wanted,
            max_nodes_per_zone=per_zone,
            add_starter=True,
        )
        zones = pack["zones"]
        nodes = pack["nodes"]
        problems = list(pack["problems"])
        if not zones:
            return {
                "zones": [],
                "nodes": [],
                "actions": [],
                "zone_edges": [],
                "problems": problems,
                "raw": _clip_text(reply, 1200),
            }
        # 新区域里的地点各自在区域坐标系里摆开；区域之间再按最近原则连起来。
        nodes_by_zone: dict[str, list[dict[str, Any]]] = {
            zone.id: [
                {"id": node.id, "x": float(node.x), "y": float(node.y)}
                for node in self.world.nodes_in_zone(zone.id)
            ]
            for zone in self.world.zones
        }
        for zone in zones:
            inside = [
                node
                for node in nodes
                if str(node.get("zone_id") or "") == str(zone["id"])
            ]
            for node, (x, y) in zip(inside, grid_layout_nodes(len(inside), aspect=aspect)):
                node["x"], node["y"] = x, y
            nodes_by_zone.setdefault(str(zone["id"]), []).extend(inside)
        all_zone_dicts = [
            {
                "id": zone.id,
                "x": float(zone.x),
                "y": float(zone.y),
                "is_home": bool(getattr(zone, "is_home", False)),
            }
            for zone in self.world.zones
        ] + [
            {
                "id": str(zone["id"]),
                "x": float(zone["x"]),
                "y": float(zone["y"]),
                "is_home": bool(zone.get("is_home")),
            }
            for zone in zones
        ]
        new_zone_ids = {str(zone["id"]) for zone in zones}
        added_edges = connect_zone_graph(
            all_zone_dicts,
            nodes_by_zone,
            existing_edges=[
                {"from_zone": edge.from_zone, "to_zone": edge.to_zone}
                for edge in (getattr(self.world, "zone_edges", None) or [])
            ],
            preferred_zones=new_zone_ids,
        )
        zone_edges = [
            edge
            for edge in added_edges
            if edge["from_zone"] in new_zone_ids or edge["to_zone"] in new_zone_ids
        ]
        # 整张地图（含既有区域）按当前画布形状重新铺开：既有区域从原位起步，
        # 新区域被弹簧拉到该去的邻居旁边。位置随草稿一起返回，确认后前端一次性套用。
        positions = spring_layout_zones(
            all_zone_dicts,
            [
                {"from_zone": edge.from_zone, "to_zone": edge.to_zone}
                for edge in (getattr(self.world, "zone_edges", None) or [])
            ]
            + zone_edges,
            aspect=aspect,
            seed={
                str(zone["id"]): (float(zone["x"]), float(zone["y"]))
                for zone in all_zone_dicts[: len(self.world.zones)]
            },
        )
        for zone in all_zone_dicts:
            zone_id = str(zone["id"])
            if zone_id in positions:
                zone["x"], zone["y"] = positions[zone_id]
        for zone in zones:
            zone["x"], zone["y"] = positions.get(
                str(zone["id"]), (float(zone["x"]), float(zone["y"]))
            )
        actions: list[dict[str, Any]] = []
        if _wants_node_actions(actions_per_node):
            drafted = await self._draft_actions_for_nodes(
                nodes=nodes,
                zone_name="、".join(
                    str(zone.get("name") or zone.get("id")) for zone in zones
                ),
                zone_note="这些是刚生成的新区域",
                per_node=actions_per_node,
                extra_prompt=extra_prompt,
            )
            actions = drafted["actions"]
            problems.extend(drafted["problems"])
        return {
            "zones": zones,
            "nodes": nodes,
            "actions": actions,
            "zone_edges": zone_edges,
            "positions": {
                str(zone["id"]): [float(zone["x"]), float(zone["y"])]
                for zone in all_zone_dicts
            },
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def generate_world_map(
        self,
        count: Any = 0,
        nodes_per_zone: Any = 0,
        extra_prompt: str = "",
        aspect: Any = None,
        actions_per_node: Any = 0,
    ) -> dict[str, Any]:
        """按人设一键重建整张地图：区域、地点、区内路线与跨区路线一次配好。

        这是**草稿**：用户在编辑器里确认后才会替换当前地图。默认地图会被换掉，
        所以调用方必须把"会替换"讲清楚。
        ``aspect`` 是编辑器画布的宽高比，决定整张图铺开的形状。
        ``actions_per_node`` > 0 时给每个新地点顺带起草几条地点专属动作，
        让"重建地图"和"这个地方能做什么"一次配齐。
        """

        wanted = clamp_map_zone_count(count)
        per_zone = clamp_map_nodes_per_zone(
            nodes_per_zone, default=DEFAULT_MAP_NODES_PER_ZONE
        )
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_world_map_generator_prompt(
            persona_text=await self._persona_text(session_id),
            relations=self.relations_brief(session_id),
            count=wanted,
            nodes_per_zone=per_zone,
            extra_prompt=extra_prompt,
        )
        reply = await self._ask_creator(
            session_id, system_prompt, prompt, raise_errors=True
        )
        draft = parse_generated_world_map(
            reply,
            max_zones=wanted,
            max_nodes_per_zone=per_zone,
        )
        zones = draft["zones"]
        nodes = draft["nodes"]
        problems = list(draft["problems"])
        if not zones or not nodes:
            return {
                "zones": zones,
                "nodes": nodes,
                "actions": [],
                "edges": [],
                "zone_edges": [],
                "problems": problems or ["没有生成出可用的地图"],
                "raw": _clip_text(reply, 1200),
            }

        # 模型给的跨区路线两端最好都是入口 / 门口这类门户；两端都不像就提醒用户改，
        # 不直接改它的路线（用户可能有意这么连），但别让它悄悄溜过去。
        node_by_id = {str(node.get("id") or ""): node for node in nodes}
        for edge in draft["connections"]:
            left = node_by_id.get(str(edge.get("from_node") or ""))
            right = node_by_id.get(str(edge.get("to_node") or ""))
            if left is None or right is None:
                continue
            if not looks_like_portal(left) and not looks_like_portal(right):
                problems.append(
                    "跨区路线「"
                    f"{left.get('name') or left.get('id')} ↔ {right.get('name') or right.get('id')}"
                    "」两端都不像入口 / 门户，建议改成门口、玄关、站台这类地点"
                )

        # 先按模型给的跨区路线做弹簧布局：有路的区域靠近、没路的推开，
        # 这样复杂路线能保留，线也不会横七竖八。
        positions = spring_layout_zones(zones, draft["connections"], aspect=aspect)
        for zone in zones:
            x, y = positions.get(str(zone["id"]), (70.0, 70.0))
            zone["x"], zone["y"] = x, y
        nodes_by_zone: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            nodes_by_zone.setdefault(str(node.get("zone_id") or ""), []).append(node)
        for zone in zones:
            inside = nodes_by_zone.get(str(zone["id"]), [])
            for node, (x, y) in zip(inside, grid_layout_nodes(len(inside), aspect=aspect)):
                node["x"], node["y"] = x, y

        edges: list[dict[str, Any]] = []
        for zone in zones:
            inside = nodes_by_zone.get(str(zone["id"]), [])
            pairs = link_plan(
                [str(node["id"]) for node in inside],
                positions={
                    str(node["id"]): (
                        float(node.get("x") or 0),
                        float(node.get("y") or 0),
                    )
                    for node in inside
                },
            )
            edges.extend(
                {
                    "id": f"e_{src}_{dst}",
                    "from": src,
                    "to": dst,
                    "ticks": 1,
                    "bidirectional": True,
                }
                for src, dst in pairs
            )

        # 模型给的跨区路线全部保留（复杂路线照旧能走）；
        # 只给完全没连上的区域补一条最近的，保证地图没有孤岛。
        zone_edges: list[dict[str, Any]] = list(draft["connections"])
        used_pairs: set[frozenset[str]] = {
            frozenset(
                (str(edge.get("from_zone") or ""), str(edge.get("to_zone") or ""))
            )
            for edge in zone_edges
        }
        for edge in connect_zone_graph(
            [
                {
                    "id": str(zone["id"]),
                    "x": float(zone["x"]),
                    "y": float(zone["y"]),
                }
                for zone in zones
            ],
            nodes_by_zone,
            existing_edges=zone_edges,
        ):
            pair = frozenset(
                (str(edge.get("from_zone") or ""), str(edge.get("to_zone") or ""))
            )
            if pair in used_pairs:
                continue
            used_pairs.add(pair)
            zone_edges.append(edge)
        if not zone_edges and len(zones) > 1:
            problems.append("跨区路线没能自动补全，请进地图检查区域落脚点")
        actions: list[dict[str, Any]] = []
        if _wants_node_actions(actions_per_node):
            drafted = await self._draft_actions_for_nodes(
                nodes=nodes,
                zone_name="、".join(
                    str(zone.get("name") or zone.get("id")) for zone in zones
                ),
                zone_note="这是刚重建出来的整张地图",
                per_node=actions_per_node,
                extra_prompt=extra_prompt,
            )
            actions = drafted["actions"]
            problems.extend(drafted["problems"])
        return {
            "zones": zones,
            "nodes": nodes,
            "actions": actions,
            "edges": edges,
            "zone_edges": zone_edges,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def generate_zone_draft(
        self,
        zone_id: str,
        count: Any = 0,
        extra_prompt: str = "",
        actions_per_node: Any = 0,
        replace: bool = False,
    ) -> dict[str, Any]:
        """重做**一个**区域：起草一批新地点，可选顺带起草地点专属动作。

        默认只追加（这个区域原有的地点留着，用户想删再删）；``replace=True`` 时把
        现有地点一并列进 ``remove_nodes``，编辑器确认时连边一起清掉并提醒改动作。
        返回的一律是草稿，不落库。
        """

        zone = self.world.zone_map().get(str(zone_id or ""))
        if zone is None:
            return {
                "zone": str(zone_id or ""),
                "nodes": [],
                "edges": [],
                "zone_edges": [],
                "actions": [],
                "remove_nodes": [],
                "problems": ["找不到这个区域"],
            }
        drafted = await self.generate_nodes_for_zone(zone_id, count, extra_prompt)
        nodes = list(drafted.get("nodes") or [])
        problems = list(drafted.get("problems") or [])
        remove_nodes = (
            [node.id for node in self.world.nodes_in_zone(zone_id)] if replace else []
        )
        actions: list[dict[str, Any]] = []
        if nodes and _wants_node_actions(actions_per_node):
            made = await self._draft_actions_for_nodes(
                nodes=nodes,
                zone_name=zone.name or zone.id,
                zone_note=zone.note,
                per_node=actions_per_node,
                extra_prompt=extra_prompt,
            )
            actions = made["actions"]
            problems.extend(made["problems"])
        edges = list(drafted.get("edges") or [])
        zone_edges: list[dict[str, Any]] = []
        if replace and nodes:
            node_positions = {
                str(node["id"]): (
                    float(node.get("x") or 0),
                    float(node.get("y") or 0),
                )
                for node in nodes
            }
            edges = [
                {
                    "id": f"e_{source}_{target}",
                    "from": source,
                    "to": target,
                    "ticks": 1,
                    "bidirectional": True,
                }
                for source, target in link_plan(
                    [str(node["id"]) for node in nodes],
                    positions=node_positions,
                )
            ]
            nodes_by_zone = {
                item.id: [
                    {
                        "id": node.id,
                        "name": node.name or node.id,
                        "prompt": node.prompt,
                        "x": float(node.x),
                        "y": float(node.y),
                    }
                    for node in self.world.nodes_in_zone(item.id)
                ]
                for item in self.world.zones
            }
            nodes_by_zone[zone_id] = [
                {
                    "id": str(node["id"]),
                    "name": str(node.get("name") or node["id"]),
                    "prompt": str(node.get("prompt") or ""),
                    "x": float(node.get("x") or 0),
                    "y": float(node.get("y") or 0),
                }
                for node in nodes
            ]
            retained_zone_edges = [
                {
                    "from_zone": edge.from_zone,
                    "to_zone": edge.to_zone,
                }
                for edge in self.world.zone_edges
                if zone_id not in (edge.from_zone, edge.to_zone)
            ]
            zone_edges = [
                edge
                for edge in connect_zone_graph(
                    [
                        {
                            "id": item.id,
                            "x": float(item.x),
                            "y": float(item.y),
                        }
                        for item in self.world.zones
                    ],
                    nodes_by_zone,
                    existing_edges=retained_zone_edges,
                    preferred_zone=zone_id,
                )
                if zone_id in (edge["from_zone"], edge["to_zone"])
            ]
        return {
            "zone": str(zone_id),
            "zone_name": zone.name or zone.id,
            "nodes": nodes,
            "edges": edges,
            "zone_edges": zone_edges,
            "actions": actions,
            "remove_nodes": remove_nodes,
            "problems": problems,
            "raw": drafted.get("raw", ""),
        }

    async def generate_zone_chunk(
        self,
        index: Any = 0,
        total: Any = 0,
        nodes_per_zone: Any = 0,
        actions_per_node: Any = 0,
        extra_prompt: str = "",
        existing: dict[str, Any] | None = None,
        aspect: Any = None,
    ) -> dict[str, Any]:
        """分块重建地图：**一次只生成一个区域**，并接到已有地图上。

        整张地图一次生成要几分钟，普通 HTTP 请求很容易被反向代理
        （Cloudflare 默认 100 秒）掐断。按区域切开之后每块都短；后一块能读到
        前面已经生成的区域 / 地点 / 路线，世界观和连通性反而更稳。

        ``existing`` 是前端累积的草稿：``zones`` / ``nodes`` / ``zone_edges``，
        一律只读，不改它。
        """

        payload = existing if isinstance(existing, dict) else {}
        try:
            order = max(1, int(index) + 1)  # 前端传的是 0-based
        except (TypeError, ValueError):
            order = 1
        try:
            total_count = max(order, int(total))
        except (TypeError, ValueError):
            total_count = order
        per_zone = clamp_map_nodes_per_zone(
            nodes_per_zone, default=DEFAULT_MAP_NODES_PER_ZONE
        )
        # 已有地点按区域分组：提示词要列"每个区域有哪些地点"，补线也要挑门户节点
        nodes_by_zone: dict[str, list[dict[str, Any]]] = {}
        existing_nodes: dict[str, str] = {}
        for item in payload.get("nodes") or []:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "")
            zone_id = str(item.get("zone_id") or "")
            if not node_id or not zone_id:
                continue
            existing_nodes[node_id] = zone_id
            nodes_by_zone.setdefault(zone_id, []).append(
                {"id": node_id, "name": str(item.get("name") or node_id)}
            )
        existing_zones: list[dict[str, Any]] = []
        for item in payload.get("zones") or []:
            if not isinstance(item, dict):
                continue
            zone_id = str(item.get("id") or "")
            if not zone_id:
                continue
            existing_zones.append(
                {
                    "id": zone_id,
                    "name": str(item.get("name") or zone_id),
                    "note": str(item.get("note") or ""),
                    "x": float(item.get("x") or 0.0),
                    "y": float(item.get("y") or 0.0),
                    "is_home": bool(item.get("is_home")),
                    "nodes": nodes_by_zone.get(zone_id, []),
                }
            )
        existing_edges = [
            {
                "from_zone": str(item.get("from_zone") or ""),
                "to_zone": str(item.get("to_zone") or ""),
            }
            for item in payload.get("zone_edges") or []
            if isinstance(item, dict)
            and item.get("from_zone")
            and item.get("to_zone")
        ]
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_zone_chunk_prompt(
            persona_text=await self._persona_text(session_id),
            relations=self.relations_brief(session_id),
            index=order,
            total=total_count,
            nodes_per_zone=per_zone,
            extra_prompt=extra_prompt,
            existing_zones=existing_zones,
            existing_zone_edges=existing_edges,
        )
        reply = await self._ask_creator(
            session_id, system_prompt, prompt, raise_errors=True
        )
        draft = parse_generated_world_map(
            reply,
            max_zones=1,
            max_nodes_per_zone=per_zone,
            known_nodes=existing_nodes,
            known_zones={zone["id"] for zone in existing_zones},
        )
        zones = list(draft["zones"])
        nodes = list(draft["nodes"])
        problems = list(draft["problems"])
        connections = list(draft["connections"])
        if not zones:
            return {
                "zone": None,
                "nodes": [],
                "edges": [],
                "zone_edges": [],
                "actions": [],
                "problems": problems or ["没有生成出可用的区域"],
                "raw": _clip_text(reply, 1200),
            }
        zone = zones[0]
        zone_id = str(zone.get("id") or "")
        # 只有第一块是家：分块生成时不允许模型在后面又造一个"家"
        zone["is_home"] = order == 1
        inside = [
            node for node in nodes if str(node.get("zone_id") or "") == zone_id
        ]
        for node, (x, y) in zip(inside, grid_layout_nodes(len(inside), aspect=aspect)):
            node["x"], node["y"] = x, y
        nodes_by_zone[zone_id] = [
            {"id": str(node.get("id") or ""), "name": str(node.get("name") or "")}
            for node in inside
            if node.get("id")
        ]
        # 只保留和新区域有关的跨区路线；连到两个老区域的不要
        connections = [
            edge
            for edge in connections
            if zone_id in (edge.get("from_zone"), edge.get("to_zone"))
        ]
        if existing_zones and not connections:
            problems.append("模型没给新区域的入口连线，已自动补一条")
        # 新区域补线：只把"涉及新区域"的那几条留下（老区域之间的历史问题不在这里修）
        added = [
            edge
            for edge in connect_zone_graph(
                [
                    {
                        "id": item["id"],
                        "x": item["x"],
                        "y": item["y"],
                    }
                    for item in existing_zones
                ]
                + [{"id": zone_id, "x": 0.0, "y": 0.0}],
                nodes_by_zone,
                existing_edges=existing_edges
                + [
                    {"from_zone": item["from_zone"], "to_zone": item["to_zone"]}
                    for item in connections
                ],
                preferred_zone=zone_id,
            )
            if zone_id in (edge.get("from_zone"), edge.get("to_zone"))
        ]
        connections.extend(added)
        # 区域位置：老区域原地不动，新区域被弹簧拉到它的邻居旁边
        placed = [
            {
                "id": item["id"],
                "x": item["x"],
                "y": item["y"],
                "is_home": item["is_home"],
            }
            for item in existing_zones
        ] + [{"id": zone_id, "x": 0.0, "y": 0.0, "is_home": zone["is_home"]}]
        positions = spring_layout_zones(
            placed,
            [
                {"from_zone": item["from_zone"], "to_zone": item["to_zone"]}
                for item in existing_edges + connections
            ],
            aspect=aspect,
            seed={item["id"]: (item["x"], item["y"]) for item in existing_zones},
        )
        x, y = positions.get(zone_id, (260.0, 260.0))
        zone["x"], zone["y"] = x, y
        # 每次把**整张图**的位置一起返回：只挪新区域的话，上一块算出来的坐标
        # 和新一块不在同一套画框里，几个区域会叠在同一个角上（见 #1）。
        all_positions = {
            str(item): [float(point[0]), float(point[1])]
            for item, point in positions.items()
        }
        edges = [
            {
                "id": f"e_{src}_{dst}",
                "from": src,
                "to": dst,
                "ticks": 1,
                "bidirectional": True,
            }
            for src, dst in link_plan(
                [str(node["id"]) for node in inside],
                positions={
                    str(node["id"]): (
                        float(node.get("x") or 0),
                        float(node.get("y") or 0),
                    )
                    for node in inside
                },
            )
        ]
        actions: list[dict[str, Any]] = []
        if inside and _wants_node_actions(actions_per_node):
            made = await self._draft_actions_for_nodes(
                nodes=inside,
                zone_name=str(zone.get("name") or zone_id),
                zone_note=str(zone.get("note") or ""),
                per_node=actions_per_node,
                extra_prompt=extra_prompt,
            )
            actions = made["actions"]
            problems.extend(made["problems"])
        return {
            "index": order,
            "zone": zone,
            "nodes": inside,
            "edges": edges,
            "zone_edges": connections,
            "actions": actions,
            "positions": all_positions,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def generate_schedules(
        self, count: Any = 0, flavor: str = "", extra_prompt: str = ""
    ) -> dict[str, Any]:
        """按她的生活节奏生成日程草稿（不落库，先给用户改）。

        ``flavor="once"`` 时排的是**一次性安排**：模型写 once + date，
        跑完那一遍插件会把它删掉；默认排的是每天循环的日常作息。
        """

        wanted = clamp_schedule_count(count)
        session_id = self._generator_session_id()
        actions = [
            {
                "id": action.id,
                "name": action.name or action.id,
                "desc": action.event_text or action.description,
            }
            for action in self.world.actions
            if action.enabled and not bool(getattr(action, "legacy", False))
            and not (action.id == "sleep" and self.world.sleep.mode == "autonomous")
        ]
        existing = []
        if self.schedules is not None:
            existing = [
                {"id": item.id, "time": item.time, "note": item.note}
                for item in (self.schedules.schedules or [])
            ]
        life_lines = [f"当前时间：{self.local_now().strftime('%Y-%m-%d %H:%M')}"]
        if self.world.sleep.mode == "autonomous":
            life_lines.append("整觉时间由独立自主作息决定，日程只安排生活活动，不安排固定入睡；留出休息空间。")
        if session_id:
            state = await self.load_state(session_id, cold_start=False)
            life_lines.append(f"地点：{self.node_label(state.node_id)}；状态：{state.state}；精力：{state.energy:.2f}")
            life_lines.extend(self.prompts._event_line(event) for event in
                              (state.recent_events or [])[-8:] if isinstance(event, dict))
            life_lines.append(self._own_topic_block(state))
            life_lines.append("生活事项有独立的推进安排；日程请为它们留出空档，不要重复建立同一项目的动作或把规划当成完成。")
        system_prompt, prompt = self.prompts.build_schedule_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zones=[
                {"id": zone.id, "name": zone.name or zone.id, "note": zone.note}
                for zone in self.world.zones
            ],
            nodes=[
                {
                    "id": node.id,
                    "name": node.name or node.id,
                    "zone": self.world.zone_of(node.id),
                }
                for node in self.world.nodes
            ],
            actions=actions,
            existing_schedules=existing,
            relations=self.relations_brief(session_id),
            count=wanted,
            flavor=str(flavor or ""),
            today=(
                self._today_key(self._now())
                if str(flavor or "") == "once"
                else ""
            ),
            extra_prompt=str(extra_prompt or ""),
            life_context="\n".join(line for line in life_lines if line),
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt, raise_errors=True)
        schedules, problems = parse_generated_schedules(
            reply,
            existing_ids={item["id"] for item in existing},
            action_ids={item["id"] for item in actions},
            node_ids={node.id for node in self.world.nodes},
            max_schedules=wanted,
        )
        return {
            "schedules": schedules,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    AUTO_SCHEDULE_KV = "schedule.auto_day"
    """「今天已经自动排过日程了吗」记在 kv 里，按配置范围分开。"""

    async def maybe_auto_generate_schedules(self) -> str:
        async with self._auto_schedule_lock:
            return await self._auto_generate_schedules()

    async def _auto_generate_schedules(self) -> str:
        """到点自动排今天的**一次性**日程（每个配置范围每天只排一次）。

        用户在日程页打开「自动生成」之后，每天到了设定的时间点，就用同一个
        内容生成模型排几条一次性安排：它们和手动生成的没区别——按地图与人设
        写出来，带日期，跑完那一次插件会自动删掉，不会越积越多。
        返回一句说明（日志用），空串表示这次什么都没做。
        """

        config = getattr(self.schedules, "auto", None)
        if config is None or not bool(getattr(config, "enabled", False)):
            return ""
        now = self.local_now()
        try:
            hour = max(0, min(23, int(getattr(config, "hour", 8) or 0)))
        except (TypeError, ValueError):
            hour = 8
        if now.hour < hour:
            return ""
        scope_key = self.config_scope_id or "global"
        today = self._today_key(self._now())
        kv_key = f"{self.AUTO_SCHEDULE_KV}:{scope_key}"
        attempt_key = f"{kv_key}:attempt"
        try:
            if str(await self.db.call("kv_get", kv_key) or "") == today:
                return ""
        except Exception:
            pass
        # Persist a bounded retry window. A provider outage must not consume the
        # whole day's plan, and restarting must not reset the retry budget.
        attempt = await self.db.call("kv_get", attempt_key) or {}
        if not isinstance(attempt, dict) or attempt.get("day") != today:
            attempt = {"day": today, "count": 0, "retry_at": 0}
        count = int(attempt.get("count") or 0)
        if count >= 3 or self._now() < float(attempt.get("retry_at") or 0):
            return ""
        await self.db.call("kv_set", attempt_key, {
            "day": today, "count": count + 1,
            "retry_at": self._now() + 900 * (3 ** count),
        })
        try:
            wanted = max(1, min(6, int(getattr(config, "per_day", 2) or 2)))
        except (TypeError, ValueError):
            wanted = 2
        data = self.scoped_schedules() or {}
        rows = [item for item in (data.get("schedules") or []) if isinstance(item, dict)]
        # 顺手清掉过期没跑成的一次性安排：留着也不会再触发，只会挡着新日程
        kept = [
            item
            for item in rows
            if not (
                item.get("once")
                and str(item.get("date") or "")
                and str(item.get("date")) < today
            )
        ]
        dropped = len(rows) - len(kept)
        result = await self.generate_schedules(
            wanted, "once", str(getattr(config, "extra_prompt", "") or "")
        )
        drafts = list(result.get("schedules") or [])
        used = {str(item.get("id") or "") for item in kept}
        added = 0
        for draft in drafts:
            item = dict(draft)
            # The automated job can only create bounded one-off plans, even if
            # the provider forgets `once` or proposes a time already passed.
            current = self.local_now()
            tomorrow = (current + timedelta(days=1)).date().isoformat()
            item["once"] = True
            item["date"] = str(item.get("date") or today)
            if item["date"] not in {today, tomorrow}:
                continue
            if item["date"] == today and str(item.get("time") or "") <= current.strftime("%H:%M"):
                item["date"] = tomorrow
            base = str(item.get("id") or "auto_plan")
            candidate = base
            index = 2
            while candidate in used:
                candidate = f"{base}_{index}"
                index += 1
            item["id"] = candidate
            used.add(candidate)
            kept.append(item)
            added += 1
        if not added and not dropped:
            self._log("info", "[my_world] 自动排日程：这次没生成出可用的安排")
            return "自动排日程：这次没生成出可用的安排"
        data["schedules"] = kept
        self.save_scoped_schedules(data, reason="自动生成一次性日程")
        self.reload_config()
        if added:
            await self.db.call("kv_set", kv_key, today)
        note = f"自动排了 {added} 条一次性日程"
        if dropped:
            note += f"，清掉 {dropped} 条过期没跑的"
        self._log("info", f"[my_world] {note}")
        return note

    async def auto_generate_schedule_scopes(self) -> None:
        """Generate global plans once and each independent schedule library once."""

        scopes = [""]
        for session_id in self.tick_session_ids():
            with self.config_scope(session_id):
                session = self.session_config(self.config_scope_id)
                overrides = getattr(session, "config_overrides", None) or {}
                if "schedules" in overrides and self.config_scope_id not in scopes:
                    scopes.append(self.config_scope_id)
        for scope in scopes:
            try:
                with self.config_scope(scope):
                    await self.maybe_auto_generate_schedules()
            except Exception as exc:
                self._log("warning", f"自动排日程失败 scope={scope or 'global'}（有限重试）：{exc}")

    async def generate_events(
        self, count: Any = 0, flavor: str = "", extra_prompt: str = ""
    ) -> dict[str, Any]:
        """按她的人设与最近经历生成事件草稿（不落库，先给用户改）。

        ``flavor`` 是这一批事件的倾向：``good`` 好事 / ``bad`` 糟心事 /
        ``weird`` 怪事 / ``daily`` 日常小事；留空就是混着来（默认）。
        """

        wanted = clamp_event_count(count)
        session_id = self._generator_session_id()
        recent: list[str] = []
        try:
            state = (
                await self.load_state(session_id, cold_start=False)
                if session_id
                else None
            )
            if state is not None:
                recent = [
                    line
                    for line in (
                        self.prompts._event_line(item)
                        for item in (state.recent_events or [])
                        if isinstance(item, dict)
                    )
                    if line
                ][-8:]
        except Exception:
            recent = []
        system_prompt, prompt = self.prompts.build_event_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zones=[
                {"id": zone.id, "name": zone.name or zone.id, "note": zone.note}
                for zone in self.world.zones
            ],
            nodes=[
                {"id": node.id, "name": node.name or node.id}
                for node in self.world.nodes
            ],
            recent_events=recent,
            relations=self.relations_brief(session_id),
            count=wanted,
            flavor=str(flavor or ""),
            extra_prompt=str(extra_prompt or ""),
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt, raise_errors=True)
        events, problems = parse_generated_events(reply, max_events=wanted)
        return {
            "events": events,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def deliver_generated_event(
        self, session_id: str, text: str
    ) -> str:
        """把编辑器里挑好的事件草稿投递出去（等价于 ``/mw event <一句话>``）。"""

        body = " ".join(str(text or "").split())
        if not body:
            return "事件内容是空的。"
        return await self.submit_event(session_id, body)

    async def set_values(
        self, session_id: str, values: dict[str, Any]
    ) -> dict[str, float]:
        """手改她的数值（编辑器「实时状态」用）：只认已知字段，自动夹到 0~1，并记一条日志。"""

        allowed = (
            "energy",
            "loneliness",
            "curiosity",
            "affect",
            "valence",
            "boredom",
            "desire",
        )
        applied: dict[str, float] = {}
        async with self.session_state(session_id) as state:
            # Settle the past before applying the user's new values.
            self.dynamics._sync_emotions(state, now=self._now())
            for key, value in (values or {}).items():
                if key not in allowed:
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(number):
                    continue
                number = max(0.0, min(1.0, number))
                if key != "valence":
                    setattr(state, key, number)
                applied[key] = round(number, 4)
            if applied:
                state.affect_synced_at = self._now()
                self.dynamics.refresh(state, now=self._now())
                if "valence" in applied:
                    # Needs change its baseline: set valence after all other fields.
                    self.dynamics.apply_effects(state, {"valence": f"={applied['valence']}"}, now=self._now())
                    applied["valence"] = round(state.valence, 4)
                if state.loneliness < 0.7:
                    state.high_loneliness_since = 0
                state.mood = self.dynamics.derive_mood(state)
                await self._log_event(state, "manual", {"values": applied})
        return applied

    async def apply_cancel(
        self,
        state: WorldState,
        mode: str,
        user_text: str,
        outcome: TickOutcome,
    ) -> str:
        """按模型给出的 ``cancel`` 停掉她的手头安排。

        - ``now``：立刻停手（尊重「可打断」标记），并放弃剩下的安排；
        - ``queue``：手上这件做完，但不按原计划继续了。

        模型既然判断要终止，就照做——否则会出现"嘴上说不干了、身体还在接着干"。
        ``user_text`` 只用来记日志（谁说了什么让她停的）。返回做了什么（空串 = 什么都没做）。
        """

        if mode not in CANCEL_MODES:
            return ""

        action = state.current_action if isinstance(state.current_action, dict) else None
        had_plan = isinstance(state.current_plan, dict) and bool(
            state.current_plan.get("steps")
        )
        stopped = ""
        blocked = ""
        if mode == "now" and action is not None:
            if action.get("interruptible", True):
                stopped = str(action.get("type") or "")
                if stopped in {"sleep", "nap"}:
                    self._finish_sleep_rhythm(state, action, interrupted=True)
                self.pause_matter_action(state, action)
                state.current_action = None
                state.state = STATE_IDLE
            else:
                # 标了「不可打断」的动作（例如睡觉）不做一半就扔，但剩下的安排照样放弃
                blocked = str(action.get("type") or "")
        state.current_plan = None
        cancelled_sleep = self._cancel_pending_sleep(state, reason="待睡安排已取消，稍后重新评估")

        parts = []
        if cancelled_sleep:
            parts.append("取消了待入睡安排")
        if stopped:
            definition = self.world.action_map().get(stopped)
            label = (definition.name or definition.id) if definition else stopped
            parts.append(f"停掉了「{label}」")
        if blocked:
            definition = self.world.action_map().get(blocked)
            label = (definition.name or definition.id) if definition else blocked
            parts.append(f"「{label}」不可打断，做完这件就停")
        if had_plan:
            parts.append("放弃了还没做的安排")
        if not parts:
            parts.append("本来就没在忙")
        note = "、".join(parts)
        outcome.notes.append(f"要她停下：{note}")
        await self._log_event(
            state,
            "cancel",
            {
                "mode": mode,
                "stopped": stopped,
                "blocked": blocked,
                "plan": had_plan,
                "by": str(user_text or "")[:60],
                "note": note,
            },
            outcome=outcome,
        )
        return note

    async def interrupt(self, session_id: str, *, force: bool = False) -> bool:
        """打断当前持续动作。返回是否真的打断了。

        **打断睡觉 = 叫醒**：只把动作停掉是不够的——计划里那一步还在，下一个 tick
        又会把她放倒（看起来就是"打断了却马上又睡了，@ 她也没反应"）。
        所以这里走和「叫醒」同一套：停动作、清掉排队的计划、给一段不会再睡的保护期。
        """

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            action = state.current_action
            asleep = self._is_asleep(state) or state.state == STATE_DROWSY
            if asleep:
                self._wake_up_state(
                    state, MessageContext(session_id=session_id, user_name="你", text="")
                )
                return True
            if not isinstance(action, dict):
                return False
            if not force and not action.get("interruptible", True):
                return False
            if action.get("type") in {"sleep", "nap"}:
                self._finish_sleep_rhythm(state, action, interrupted=True)
            self.pause_matter_action(state, action)
            if (state.current_plan or {}).get("matter_id") == action.get("matter_id") and action.get("matter_id"):
                state.current_plan = None
            state.add_event("interrupt", {"action": action.get("type")})
            state.current_action = None
            state.state = STATE_IDLE
            return True

    async def clear_plan(self, session_id: str) -> bool:
        """放弃还没做的安排（编辑器按钮与 /mw stop 用）。返回是否有东西被清掉。"""

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            plan = state.current_plan
            had_plan = isinstance(plan, dict) and bool(plan.get("steps"))
            cancelled_sleep = self._cancel_pending_sleep(state, reason="待睡安排已取消，稍后重新评估")
            if not had_plan and not cancelled_sleep:
                return False
            state.current_plan = None
            await self._log_event(
                state, "cancel", {"mode": "queue", "plan": had_plan,
                    "note": "取消了待入睡安排，稍后重新评估" if cancelled_sleep else "放弃了还没做的安排"}
            )
            return True

    async def live_group_card(
        self, session_id: str, *, ttl: float = 30.0, force: bool = False
    ) -> str:
        """读平台上的**真实**群名片（带短缓存）。

        dynamic_card_plus 这类插件会自己改名，插件记录的那份可能早就过期；编辑器每次
        刷新状态都直接打平台接口又太重，所以 ``ttl`` 秒内复用同一次结果。
        ``force=True``（「重新获取」按钮）绕过缓存。
        """

        if ":GroupMessage:" not in session_id or self.messenger is None:
            return ""
        fetch = getattr(self.messenger, "fetch_group_card", None)
        if not callable(fetch):
            return ""
        now = time.monotonic()
        cached = self._live_card_cache.get(session_id)
        if cached and not force and (now - float(cached[0])) < max(0.0, float(ttl)):
            return str(cached[1] or "")
        card = ""
        try:
            card = str(await fetch(session_id) or "").strip()
        except Exception as exc:
            self._log("debug", f"读取群名片失败：{exc}")
        self._live_card_cache[session_id] = (now, card)
        return card

    def nickname_owner(self, session_id: str) -> str:
        """群名片现在由谁管理（dynamic_card_plus 接管时返回它的名字）。"""

        if self.messenger is None:
            return ""
        getter = getattr(self.messenger, "group_card_owner", None)
        if not callable(getter):
            return ""
        try:
            return str(getter(session_id) or "")
        except Exception:
            return ""

    def note_local_card(self, session_id: str, card: str) -> None:
        """插件自己刚把名片写成 ``card``：把真实名片缓存同步过来，编辑器立刻能读到。"""

        self._live_card_cache[session_id] = (
            time.monotonic(),
            str(card or "").strip(),
        )

    async def align_owned_nickname(self, session_id: str, card: str) -> bool:
        """名片被别的插件接管时，把本地记录对齐到平台上的真实值。

        这修的是「第一次改过的名字一直卡在编辑器里」：以前保存名片会顺手写
        ``bot_base_nickname``，而控制台优先显示 base。被别人接管时 base 本来也没有意义，
        跟着平台上的真实名片走最不容易出错。
        """

        body = str(card or "").strip()
        if not body:
            return False
        changed = False
        async with self.session_state(session_id) as state:
            if state.bot_current_nickname != body:
                state.bot_current_nickname = body
                changed = True
            if state.bot_base_nickname != body:
                state.bot_base_nickname = body
                changed = True
            if changed:
                state.last_nickname_update_at = self._now()
                await self._log_event(
                    state,
                    "nickname",
                    {
                        "live": body,
                        "note": "名片由别的插件管理，按平台上的真实名片对齐",
                    },
                )
        return changed

    async def refresh_nickname(self, session_id: str) -> dict[str, Any]:
        """重新从平台读一次她现在的群名片（编辑器「重新获取」按钮用）。"""

        if not self.is_enabled(session_id):
            return {"ok": False, "card": "", "note": "这个会话没有启用她的世界"}
        card = await self.live_group_card(session_id, force=True)
        owner = self.nickname_owner(session_id)
        async with self.session_state(session_id) as state:
            before = state.bot_current_nickname
            if card:
                state.bot_current_nickname = card
                if not state.bot_base_nickname or owner:
                    # 之前没记过原名，就把这次读到的当成原名；
                    # 被别人接管时"原名"没有意义，也按真实名片对齐（修掉旧数据里卡住的别名）
                    state.bot_base_nickname = card
                state.last_nickname_update_at = self._now()
            note = "" if card else "还没收到过这个群的消息，拿不到机器人句柄"
            await self._log_event(
                state,
                "nickname",
                {"manual": True, "to": card, "ok": bool(card), "note": note},
            )
        return {
            "ok": bool(card),
            "card": card,
            "before": before,
            "note": note,
            "owner": owner,
        }

    async def clear_all_states(self) -> int:
        """清掉所有会话的世界状态（切换预设时用）。记忆与日志不动。"""

        cleared = 0
        for session_id in list(self.state_session_ids()):
            try:
                await self.db.call("delete_state", session_id)
                cleared += 1
            except Exception as exc:
                self._log("warning", f"清状态失败 {session_id}: {exc}")
        self._event_ids.clear()
        self._last_decider_at.clear()
        return cleared

    async def repair_states_after_config_change(self) -> list[str]:
        """换过世界（地图 / 动作）之后修一遍状态，不清状态也不让她卡住。

        换了地图还不清状态时会出现两种"卡住"：人站在一个**已经不存在的房间**里，
        或者手上挂着**已经不存在的动作 / 地点**。这里就地修好并记一条日志——
        总比让她在虚空里发呆、或者每轮都去执行一个被丢弃的步骤要好。
        """

        notes: list[str] = []
        for session_id in list(self.state_session_ids()):
            fixed: list[str] = []
            try:
                async with self.session_state(session_id) as state:
                    # Each character may own a different map/action block. The
                    # editor's calling scope cannot repair other lives with its map.
                    nodes = set(self.world.node_map())
                    actions = set(self.world.action_map())
                    default_node = self.default_node_id()
                    if state.node_id and state.node_id not in nodes:
                        fixed.append(f"地点 {state.node_id} 没了，落到 {default_node}")
                        state.node_id = default_node
                        state.node_since = self._now()
                    action = state.current_action
                    if isinstance(action, dict):
                        kind = str(action.get("type") or "")
                        node = str(action.get("target_node") or "")
                        if (kind and kind not in actions) or (node and node not in nodes):
                            fixed.append(f"丢掉手上的「{kind or '动作'}」")
                            self.pause_matter_action(state, action)
                            if kind in {"sleep", "nap"}:
                                self._finish_sleep_rhythm(state, action, interrupted=True)
                            state.current_action = None
                            state.state = STATE_IDLE
                    plan = state.current_plan
                    if isinstance(plan, dict) and plan.get("steps"):
                        kept: list[dict[str, Any]] = []
                        remap: dict[int, int] = {}
                        old_index = int(plan.get("current_step") or 0)
                        for index, step in enumerate(plan["steps"]):
                            if not isinstance(step, dict):
                                continue
                            kind = str(step.get("action") or "")
                            node = str(step.get("target_node") or "")
                            if (kind and kind not in actions) or (node and node not in nodes):
                                continue
                            remap[index] = len(kept)
                            kept.append(step)
                        if len(kept) != len(plan["steps"]):
                            fixed.append(f"排队的计划里删掉 {len(plan['steps']) - len(kept)} 步")
                            plan["steps"] = kept
                            plan["current_step"] = sum(index < old_index for index in remap)
                            running = state.current_action
                            if running and same_plan(running, plan) and running.get("plan_index") in remap:
                                running["plan_index"] = remap[running["plan_index"]]
                            if plan["current_step"] >= len(kept):
                                state.current_plan = None
                    if fixed:
                        await self._log_event(
                            state, "repair", {"note": "；".join(fixed)}
                        )
            except Exception as exc:
                self._log("warning", f"修状态失败 {session_id}: {exc}")
            if fixed:
                notes.append(f"{self.scope_session(session_id)}：{'；'.join(fixed)}")
        return notes

    def state_session_ids(self) -> list[str]:
        """有世界状态的会话 id。"""

        try:
            return [
                str(item)
                for item in self.db.raw.list_state_ids()
                if str(item)
            ]
        except Exception:
            return []

    async def wake_up(self, session_id: str) -> bool:
        """手动叫醒（编辑器按钮 / `/mw 叫醒`）。

        走的是和「@ 她 + 唤醒词」完全一样的那套：停下动作、**清掉排队的计划**
        （否则计划里那一步「睡觉」下一个 tick 又把她放倒）、给一段不会再睡的保护期、
        顺手把群名片从「睡觉中」改回来。
        """

        if not self.is_enabled(session_id):
            return False
        async with self.session_state(session_id) as state:
            # 临睡期被叫醒 = "别睡了，再陪我一会儿"：把揣着的那一步睡觉丢掉
            was_sleeping = state.is_sleeping or state.state == STATE_DROWSY
            if not was_sleeping:
                return False
            if state.state == STATE_DROWSY:
                state.drowsy_sleep_step = {}
                state.drowsy_started_world_time = 0
                state.drowsy_started_at = 0.0
                state.state = STATE_IDLE
            self._wake_up_state(
                state, MessageContext(session_id=session_id, user_name="你", text="")
            )
            return was_sleeping
