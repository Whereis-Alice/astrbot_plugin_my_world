"""打断与唤醒。"""

from __future__ import annotations

from ..model_calls import model_operation

from typing import Any
from ..generator import (
    MAX_ACTIONS_PER_NODE,
    auto_layout,
    clamp_node_count,
    clamp_per_node,
    clamp_world_action_count,
    link_plan,
    parse_generated_actions,
    parse_generated_nodes,
)
from .util import _clip_text


class InterruptMixin:
    async def generate_idle_actions(self, count: Any = 8, extra_prompt: str = "") -> dict[str, Any]:
        """Persona-based quiet activities, drafted once and used without runtime LLM calls."""
        wanted = clamp_world_action_count(count)
        session_id = self._generator_session_id()
        config = self.world.idle_life
        persona = await self._persona_text(session_id)
        existing = [action.name or action.id for action in self.world.actions if action.idle]
        system = "为虚拟角色设计符合人设的通用闲置动作。只输出 JSON，不要解释。"
        prompt = (
            f"人设：\n{persona}\n已有闲置动作（不要重复）：{'、'.join(existing[:40])}\n"
            f"补充要求：{config.generation_prompt}\n生成 {wanted} 个随时随地可以做的独处小事。"
            "例如哼歌、伸展、整理思绪、观察周围；要符合她的个性、习惯和兴趣。"
            "不需要特定物品或地点，不涉及他人、外部工具、发送消息、重要事件。"
            "名称 2~12 字，description 一句话。life_role 选 rest/interest/explore/none。"
            "不要设定新的身世或人物关系。无需配数值效果，生活类别已有缓解逻辑。"
            '格式：{"actions":[{"id":"idle_humming","name":"轻轻哼歌",'
            '"description":"随口哼一段熟悉的旋律","life_role":"interest",'
            '"on_complete":{"effects":{}}}]}'
        )
        if str(extra_prompt or "").strip():
            prompt += "\n本次额外要求：" + str(extra_prompt).strip()
        reply = await self._ask_creator(session_id, system, prompt, raise_errors=True)
        actions, problems = parse_generated_actions(reply, node_ids=["anywhere"],
            existing_ids={action.id for action in self.world.actions},
            max_per_node=wanted, max_total=wanted, tool_names=set())
        for action in actions:
            role = action.get("life_role", "interest")
            action.update(idle=True, category="continuous", llm_level="template", scope="global",
                          allowed_nodes=[], target_type="none", group="闲置生活", visible=False,
                          interruptible=True, duration=0, duration_mode="fixed", template="",
                          tool_names=[], tool_name="", trigger_command="", intimacy=0,
                          life_role=role if role in {"none", "rest", "interest", "explore"} else "interest")
            action.pop("node_id", None)
            action["on_complete"] = {"trigger": "none", "effects": {}, "effects_per_minute": {}}
            # The life role drives needs; generated text cannot smuggle runtime commands/effects.
            action["during"] = {}
            action["preconditions"] = {}
            action["params"] = {}
        return {"actions": actions, "problems": problems, "raw": _clip_text(reply, 1200)}

    # ================= 打断 / 唤醒 =================

    @model_operation("creation", "编辑器或指令请求生成世界内容")
    async def _ask_creator(
        self, session_id: str, system_prompt: str, prompt: str, *, raise_errors: bool = False
    ) -> str | None:
        """内容生成模型：编事件包，以及编辑器里批量生成动作 / 地点。"""

        from ..recovery import recovery_note
        physiology = recovery_note(self.world)
        if physiology:
            system_prompt += "\n" + physiology
        system_prompt += (
            "\n生成动作时如涉及恢复精力，请明确 recovery_kind：sleep=整觉，nap=小睡，"
            "restore=充电/冥想等非睡眠恢复，none=无恢复。恢复动作使用 continuous，"
            "按实际持续分钟配置 on_complete.effects_per_minute.energy 为正值；"
            "避免以完成奖励直接把精力设满。睡眠类型会接入作息与唤醒，充电等不会进入睡眠。"
            "是否需要睡眠、恢复方式和动作限制以角色设定及生理配置为准。"
        )
        try:
            if self.creator_llm is None:
                raise ValueError("没有可用的内容生成模型，请在插件配置中选择模型或配置 AstrBot 默认对话模型")
            reply = await self._background_generate(self.creator_llm,
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.8,
            )
            if reply is None or not getattr(reply, "ok", True):
                raise ValueError(getattr(reply, "error", "") or "模型没有返回内容")
            text = getattr(reply, "text", "") or ""
            if not text.strip():
                raise ValueError("模型返回了空内容，请检查内容生成模型是否支持文本输出后重试")
        except Exception as exc:
            self._log("warning", f"内容生成模型调用失败: {exc}")
            if raise_errors:
                raise ValueError(f"内容生成失败：{str(exc)[:500] or type(exc).__name__}") from exc
            return None
        return text

    def _generator_session_id(self) -> str:
        """生成时借一个会话 id 去取人格（没有启用会话就用空字符串）。"""

        if self.config_scope_id:
            return self.config_scope_id
        ids = self.enabled_session_ids()
        return ids[0] if ids else ""

    async def _draft_actions_for_nodes(
        self,
        *,
        nodes: list[dict[str, Any]],
        zone_name: str,
        zone_note: str = "",
        per_node: Any = 0,
        extra_prompt: str = "",
        existing_ids: set[str] | None = None,
        replacing: bool = False,
    ) -> dict[str, Any]:
        """给一批地点生成动作草稿（地点可以还没落库：地图 / 区域生成时用同一套）。

        ``nodes`` 只要带 ``id`` / ``name`` / ``prompt`` 就行——草稿节点、配置里的
        节点都能直接喂进来，所以"一键重建地图顺带生成动作"和"给已有区域补动作"
        走的是同一条路。
        """

        picked = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("id") or ""),
                "prompt": str(item.get("prompt") or ""),
            }
            for item in (nodes or [])
            if str(item.get("id") or "").strip()
        ]
        if not picked:
            return {"actions": [], "problems": []}
        count = clamp_per_node(per_node)
        session_id = self._generator_session_id()
        system_prompt, prompt = self.prompts.build_action_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone_name,
            zone_note=zone_note,
            nodes=picked,
            existing_actions=[
                action.name or action.id
                for action in self.world.actions
                if action.enabled and not (replacing and action.scope == "node"
                    and set(action.allowed_nodes).intersection(item["id"] for item in picked))
            ],
            tools=sorted(self.available_tools()),
            per_node=count,
            extra_prompt=extra_prompt,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt, raise_errors=True)
        actions, problems = parse_generated_actions(
            reply,
            node_ids=[item["id"] for item in picked],
            tool_names=set(self.available_tools()),
            max_per_node=count,
            existing_ids=(
                existing_ids
                if existing_ids is not None
                else set() if replacing else {action.id for action in self.world.actions}
            ),
        )
        if replacing:
            used = {action.id for action in self.world.actions} | set(existing_ids or ())
            for action in actions:
                base, suffix = action["id"], 2
                while action["id"] in used:
                    action["id"] = f"{base}_{suffix}"
                    suffix += 1
                used.add(action["id"])
        return {"actions": actions, "problems": problems, "raw": _clip_text(reply, 1200)}

    async def generate_actions_for_zone(
        self,
        zone_id: str,
        per_node: Any = 0,
        node_ids: list[str] | None = None,
        extra_prompt: str = "",
        replacing: bool = False,
    ) -> dict[str, Any]:
        """按区域里的**每个地点**批量生成动作草稿（不落库，交给编辑器预览勾选）。

        只传了 ``node_ids`` 时就只为这几个地点生成（编辑器里"只给这个地点生成"）。
        ``extra_prompt`` 是用户在弹窗里补的一句要求（例如"偏安静一点"）。
        """

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"actions": [], "problems": ["找不到这个区域"]}
        inside = self.world.nodes_in_zone(zone_id)
        if node_ids:
            wanted = {str(item) for item in node_ids if str(item)}
            nodes = [node for node in inside if node.id in wanted]
        else:
            nodes = inside
        if not nodes:
            return {
                "actions": [],
                "problems": ["这个区域里还没有可用地点"],
            }
        return await self._draft_actions_for_nodes(
            nodes=[
                {
                    "id": node.id,
                    "name": node.name or node.id,
                    "prompt": node.prompt or node.atmosphere.describe(),
                }
                for node in nodes
            ],
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            per_node=per_node,
            extra_prompt=extra_prompt,
            replacing=replacing,
        )

    async def generate_world_actions(self, count: Any = 0, extra_prompt: str = "") -> dict[str, Any]:
        """按人设与**整张地图**生成一批虚拟动作草稿（动作库的全局生成入口）。

        和「按区域生成」不同：这里会读全部区域与地点，让动作分摊到不同地点，
        生成结果只是草稿，用户在动作库里勾选确认后才写进配置。
        """

        wanted = clamp_world_action_count(count)
        session_id = self._generator_session_id()
        zones = [
            {"id": zone.id, "name": zone.name or zone.id, "note": zone.note}
            for zone in self.world.zones
        ]
        zone_names = {zone["id"]: zone["name"] for zone in zones}
        nodes = [
            {
                "id": node.id,
                "name": node.name or node.id,
                "zone_id": node.zone_id,
                "zone_name": zone_names.get(node.zone_id, ""),
                "prompt": node.prompt or node.atmosphere.describe(),
            }
            for node in self.world.nodes
        ]
        if not nodes:
            return {
                "actions": [],
                "problems": ["地图里还没有地点，先建好地图再生成动作"],
            }
        system_prompt, prompt = self.prompts.build_world_action_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zones=zones,
            nodes=nodes,
            existing_actions=[
                action.name or action.id
                for action in self.world.actions
                if action.enabled and not bool(getattr(action, "legacy", False))
            ],
            count=wanted,
        )
        if str(extra_prompt or "").strip():
            prompt += "\n本次额外要求：" + str(extra_prompt).strip()
        reply = await self._ask_creator(
            session_id, system_prompt, prompt, raise_errors=True
        )
        per_node_cap = max(
            2,
            min(
                MAX_ACTIONS_PER_NODE,
                (wanted + len(nodes) - 1) // len(nodes) + 1,
            ),
        )
        actions, problems = parse_generated_actions(
            reply,
            node_ids=[node["id"] for node in nodes],
            tool_names=set(),
            max_per_node=per_node_cap,
            existing_ids={action.id for action in self.world.actions},
            max_total=wanted,
        )
        return {
            "actions": actions,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    async def generate_generic_actions(
        self, count: Any = 0, use_map: Any = True, extra_prompt: str = ""
    ) -> dict[str, Any]:
        """生成一批**通用普通动作**（动作库的「生成通用动作」）。

        和「按人设与地图生成动作」相反：这里故意不看人设，写的是普通人过日子
        都会做的小事，任何角色身上都说得通。地图可选——
        ``use_map=False`` 时连地图都不给模型看，生成完由插件自己把动作铺到地点上。
        """

        wanted = clamp_world_action_count(count)
        session_id = self._generator_session_id()
        with_map = bool(use_map)
        zones = [
            {"id": zone.id, "name": zone.name or zone.id, "note": zone.note}
            for zone in self.world.zones
        ]
        zone_names = {zone["id"]: zone["name"] for zone in zones}
        nodes = [
            {
                "id": node.id,
                "name": node.name or node.id,
                "zone_id": node.zone_id,
                "zone_name": zone_names.get(node.zone_id, ""),
                "prompt": node.prompt or node.atmosphere.describe(),
            }
            for node in self.world.nodes
        ]
        if not nodes:
            return {
                "actions": [],
                "problems": ["地图里还没有地点，先建好地图再生成动作"],
            }
        system_prompt, prompt = self.prompts.build_generic_action_generator_prompt(
            zones=zones,
            nodes=nodes,
            existing_actions=[
                action.name or action.id
                for action in self.world.actions
                if action.enabled and not bool(getattr(action, "legacy", False))
            ],
            count=wanted,
            use_map=with_map,
        )
        if str(extra_prompt or "").strip():
            prompt += "\n本次额外要求：" + str(extra_prompt).strip()
        reply = await self._ask_creator(
            session_id, system_prompt, prompt, raise_errors=True
        )
        node_ids = [node["id"] for node in nodes]
        # 不看地图时模型不写 node_id，全都会先落到第一个地点；
        # 上限按总数放，免得解析阶段就把多出来的丢掉（之后再由插件铺开）。
        per_node_cap = (
            max(
                2,
                min(
                    MAX_ACTIONS_PER_NODE,
                    (wanted + len(nodes) - 1) // len(nodes) + 1,
                ),
            )
            if with_map
            else wanted
        )
        actions, problems = parse_generated_actions(
            reply,
            node_ids=node_ids,
            tool_names=set(),
            max_per_node=per_node_cap,
            existing_ids={action.id for action in self.world.actions},
            max_total=wanted,
        )
        if not with_map and actions:
            self._spread_generic_actions(actions, nodes)
        return {
            "actions": actions,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }

    @staticmethod
    def _spread_generic_actions(
        actions: list[dict[str, Any]], nodes: list[dict[str, Any]]
    ) -> None:
        """把「不看地图」生成的动作铺到地点上：能对上名字的按名字，剩下的轮流放。

        只是把动作绑到某个地点而已，不动动作本身的内容；用户在预览里随时能改。
        """

        if not actions or not nodes:
            return
        assigned: dict[str, int] = {str(node["id"]): 0 for node in nodes}
        for action in actions:
            haystack = f"{action.get('name') or ''}{action.get('id') or ''}" \
                f"{action.get('description') or ''}"
            picked = ""
            for node in nodes:
                label = str(node.get("name") or node.get("id") or "")
                if len(label) >= 2 and any(char in haystack for char in label):
                    picked = str(node["id"])
                    break
            if not picked:
                picked = min(assigned, key=lambda key: assigned[key])
            assigned[picked] = assigned.get(picked, 0) + 1
            action["node_id"] = picked
            action["allowed_nodes"] = [picked]
            action["scope"] = "node"

    async def generate_nodes_for_zone(
        self, zone_id: str, count: Any = 0, extra_prompt: str = ""
    ) -> dict[str, Any]:
        """给区域批量生成地点草稿：自动摆位 + 补主路、近路与环路（同样不落库）。

        ``extra_prompt`` 是用户在弹窗里补的一句要求（例如"多来几个户外的地方"）。
        """

        zone = self.world.zone_map().get(zone_id)
        if zone is None:
            return {"nodes": [], "edges": [], "problems": ["找不到这个区域"]}
        wanted = clamp_node_count(count)
        session_id = self._generator_session_id()
        inside = self.world.nodes_in_zone(zone_id)
        system_prompt, prompt = self.prompts.build_node_generator_prompt(
            persona_text=await self._persona_text(session_id),
            zone_name=zone.name or zone.id,
            zone_note=zone.note,
            existing_nodes=[node.name or node.id for node in inside],
            count=wanted,
            extra_prompt=extra_prompt,
        )
        reply = await self._ask_creator(session_id, system_prompt, prompt, raise_errors=True)
        nodes, problems = parse_generated_nodes(
            reply,
            existing_ids={node.id for node in self.world.nodes},
            max_nodes=wanted,
        )
        positions = auto_layout(
            [(float(node.x), float(node.y)) for node in inside], len(nodes)
        )
        for node, (x, y) in zip(nodes, positions):
            node["zone_id"] = zone_id
            node["x"], node["y"] = x, y
        # 新地点先接进最近的门户，再按坐标补主路、近路与环路。
        anchor = ""
        if inside and positions:
            anchor = min(
                inside,
                key=lambda node: (float(node.x) - positions[0][0]) ** 2
                + (float(node.y) - positions[0][1]) ** 2,
            ).id
        geometry = {
            str(node.id): (float(node.x), float(node.y))
            for node in inside
        }
        geometry.update(
            {
                str(node["id"]): (
                    float(node.get("x") or 0),
                    float(node.get("y") or 0),
                )
                for node in nodes
            }
        )
        pairs = link_plan(
            [node["id"] for node in nodes],
            anchor=anchor,
            positions=geometry,
        )
        edges = [
            {
                "id": f"e_{src}_{dst}",
                "from": src,
                "to": dst,
                "ticks": 1,
                "bidirectional": True,
            }
            for src, dst in pairs
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "problems": problems,
            "raw": _clip_text(reply, 1200),
        }
