"""决策器（设计文档 4.5 / 4.4.10 节）。

三种决策源：规则（不调 LLM，覆盖大多数自主行为）、日程（由 ScheduleRunner 处理）、
LLM（复杂情境，低频抽样）。这里只负责「规则决策」和「是否该问 LLM」。
"""

from __future__ import annotations

import random
import time
from typing import Any

from .models import WorldConfig
from .mood import DAY_BRANCH_KEYS, day_mood_branches
from .pathfinding import find_path
from .planner import active_plan, create_plan
from .state import WorldState
from .recovery import is_sleep_action, sleep_action_kind, recovery_needed

# 「回复意愿」各项权重：越孤独、越无聊、心潮越高，越想说点什么；
# 累、正在睡、刚被冷落都会把意愿压下去。
WILLINGNESS_WEIGHTS = {
    "loneliness": 0.45,
    "boredom": 0.20,
    "curiosity": 0.10,
    "affect": 0.15,
    # 效价按「离中性多远」算：心情好加一点意愿，心情差减一点（均值不偏移）
    "valence": 0.10,
    "tired": 0.35,
    "unanswered": 0.25,
}

# 插话的两条动机（详见 rule_plan 的第 0 条）：
# - A「想被注意到」：孤独感高、而且心情不太差时才愿意主动social；
# - B「想发作」：情绪上来了、心情又差，会憋不住呛一句。
INTERJECT_SOCIAL_VALENCE_FLOOR = 0.35
INTERJECT_VENT_AROUSAL = 0.6
INTERJECT_THRESHOLD_BUMP_MAX = 0.10
INTERJECT_THRESHOLD_BUMP_CLOSED = 0.15
"""心情差 / 长期低落时，插话阈值上浮多少（更不想开口）。"""

HAPPY_THRESHOLD_DROP = 0.05
"""心情好时插话阈值往下走多少：高心潮 + 高效价才是"想找人玩"那种主动。"""

# 心情低落时，自我调节类动作（发呆、看书）的门槛下调：
# 安全阀不是"触发一个动作"，而是让她的行为自然偏向自我修复。
SELF_CARE_VALENCE = 0.4
SELF_CARE_BOREDOM = 0.55

OUTDOOR_MIN_ENERGY = 0.45
"""想出门至少得有这么多精力：累着的时候她只想窝着。"""

OUTDOOR_MIN_BOREDOM = 0.5
"""在家待腻到这条线才会想到"出去走走"（天天在家就是这么修掉的）。

比发呆那条（0.8）低：待腻了就该出门，不用等到坐不住。
"""
SELF_CARE_IDLE = 0.3

# 好奇心触发的"去查点东西"两次之间至少隔这么久：查一次只降一点点好奇心，
# 不设冷却的话她会每隔几分钟就查同一件事，群里看到的就是她在刷新闻。
SEARCH_COOLDOWN_MINUTES = 45

DESIRE_HUNGRY = 0.7
"""欲求过了这条线，她会更愿意往有人那边凑（"找人"这条分支权重抬高）。"""

DESIRE_CUDDLE = 0.7
"""欲求过了这条线，她甚至会主动凑过去要贴贴（挑一个"肢体接触"的动作做）。"""

# 白天小睡睡多久：按小睡动作自己写的「每持续 1 分钟恢复多少精力」反推，
# 睡到大约这个精力为止，再卡在动作声明的时长范围内。
NAP_TARGET_ENERGY = 0.42
NAP_FALLBACK_SECONDS = 45 * 60
NAP_MIN_SECONDS = 10 * 60
NAP_MAX_SECONDS = 60 * 60


def reply_willingness(state: WorldState, weights: dict[str, float] | None = None) -> float:
    """她对「现在开口说话」的整体意愿（0~1），供本插件的决策与外部联动共用。"""

    w = dict(WILLINGNESS_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    if state.state in ("sleeping", "napping"):
        return 0.0
    score = (
        w["loneliness"] * float(state.loneliness)
        + w["boredom"] * float(state.boredom)
        + w["curiosity"] * float(state.curiosity)
        + w["affect"] * float(state.affect)
        # 效价按"离中性多远"算：心情好更想说话，心情差更不想（不改整体均值）
        + w["valence"] * (float(state.valence) - 0.5)
        - w["tired"] * (1.0 - float(state.energy))
    )
    if int(state.unanswered_count or 0) > 0:
        score -= w["unanswered"] * min(1.0, 0.5 + 0.25 * int(state.unanswered_count))
    return max(0.0, min(1.0, score))


class Decider:
    """规则决策器。"""

    def __init__(
        self,
        world: WorldConfig,
        rng: random.Random | None = None,
        *,
        now_provider: Any = None,
        hour_provider: Any = None,
        tick_seconds: float = 60.0,
        action_check: Any = None,
        persona_key_provider: Any = None,
    ) -> None:
        self.world = world
        self.rng = rng or random.Random()
        self._now_provider = now_provider
        self._hour_provider = hour_provider
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.action_check = action_check
        self.persona_key_provider = persona_key_provider

    def _now(self) -> float:
        if self._now_provider is None:
            return time.time()
        try:
            return float(self._now_provider())
        except Exception:
            return time.time()

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    def local_hour(self) -> int:
        """现在几点（0~23）。

        拿不到时间就按白天算：宁可让她小睡一会儿，也别让她在不知几点的时候睡 8 小时。
        """

        if self._hour_provider is None:
            return 12
        try:
            return int(self._hour_provider()) % 24
        except Exception:
            return 12

    def is_night(self) -> bool:
        """是不是该睡整觉的时段（默认 23:00–07:00，在「作息与夜晚」里可改）。

        拿不到钟点时按白天算：宁可让她小睡一会儿，也别让她在不知几点的时候睡 8 小时。
        """

        return self.world.is_night_time(self.local_hour())

    def can_nap(self, state=None) -> bool:
        """小睡这条路能不能用：动作在、没停用，而且真的回得了精力。"""

        if state is not None:
            return bool(self._rest_steps(state, "nap"))
        return any(self.action_usable(a.id) and self._nap_recovery_per_minute(a) > 0
                   for a in self._rest_candidates("nap"))

    def _rest_candidates(self, kind):
        from .recovery import sleep_action_kind
        preferred = self.world.sleep_action(kind)
        return sorted((a for a in self.world.action_map().values() if a.enabled and sleep_action_kind(a) == kind),
                      key=lambda a: a.id != getattr(preferred, "id", ""))

    def recovery_plan(self, state: WorldState) -> dict[str, Any] | None:
        from .pathfinding import nearest_node, travel_cost
        from .recovery import is_sleep_action, restores_energy, recovery_seconds, recovery_gain, recovery_total_seconds
        if self.world.sleep.mode != "activity":
            return None
        graph = self.world.adjacent()
        selected = self.world.sleep.recovery_action_id
        candidates = []
        for action in self.world.action_map().values():
            if (selected and action.id != selected) or (not action.enabled or action.idle
                    or action.category != "continuous" or is_sleep_action(action)
                    or action.recovery_kind == "none"
                    or not restores_energy(action, energy=state.energy)
                    or action.llm_level not in {"template", "single"} or action.tool_list() or action.trigger_command
                    or action.target_type != "none" or action.on_complete.trigger not in {"none", "llm_followup"}):
                continue
            target = nearest_node(graph, state.node_id,
                                  [node.id for node in self.world.nodes if action.available_in(node.id)])
            if not target:
                continue
            travel_seconds = (travel_cost(graph, state.node_id, target) or 0) * self.tick_seconds
            from .recovery import recovery_cost
            walking = self.world.action_map().get("walk_to")
            cost = recovery_cost(walking or action, self.world, state)
            arrival_energy = max(0, state.energy - cost * travel_seconds / 60)
            seconds = recovery_seconds(action, arrival_energy, self.world, state)
            if recovery_gain(action, arrival_energy, self.world, seconds, state) <= 0:
                continue
            total = recovery_total_seconds(action, arrival_energy, self.world, state, tick_seconds=self.tick_seconds)
            steps = [*self._travel_then(state.node_id, target, graph), {"action": action.id, "duration": seconds}]
            plan = create_plan(steps=steps, world_time=state.world_time, now=self._now(),
                tick_seconds=self.tick_seconds, valid_for=max(3600, travel_seconds + seconds + 1800),
                reason=f"精力不足，去{action.name or action.id}恢复", source="recovery")
            if self._valid_candidate(state, plan):
                candidates.append((total == float("inf"), action.recovery_kind != "restore", travel_seconds + total, action.id, plan))
        return min(candidates, key=lambda item: item[:4])[4] if candidates else None

    def sleep_pressure_high(self, state: WorldState) -> bool:
        """困意攒够了没有（``sleep.pressure_threshold``，默认 0.85）。"""

        config = getattr(self.world, "sleep", None)
        if getattr(config, "mode", "") == "activity":
            return False
        if not bool(getattr(config, "pressure_enabled", True)):
            return False
        try:
            threshold = float(getattr(config, "pressure_threshold", 0.85) or 0.85)
        except (TypeError, ValueError):
            threshold = 0.85
        try:
            pressure = float(getattr(state, "sleep_pressure", 0.0) or 0.0)
        except (TypeError, ValueError):
            pressure = 0.0
        return pressure >= threshold

    def nap_seconds(self, state: WorldState, action=None) -> int:
        """白天小睡睡多久：按小睡动作写的恢复速度，算到够她缓过来为止。

        小睡动作写的是「每持续 1 分钟精力 +0.0015」，睡多久由计划决定；
        不按恢复量反推的话，睡 10 分钟只回来 0.015，醒来立刻又困，一天就全在打盹。
        用户把小睡的恢复速度调高了，这里的时长也会跟着变短。
        """

        action = action or self.world.sleep_action("nap")
        if action is not None and action.duration_mode == "fixed":
            return max(1, action.duration)
        low, high = NAP_MIN_SECONDS, NAP_MAX_SECONDS
        if action is not None:
            try:
                low = max(60, int(action.duration_min or NAP_MIN_SECONDS))
                high = max(low, int(action.duration_max or NAP_MAX_SECONDS))
            except (TypeError, ValueError):
                pass
        per_minute = self._nap_recovery_per_minute(action)
        need = max(0.0, NAP_TARGET_ENERGY - float(state.energy))
        minutes = need / per_minute if per_minute > 0 else NAP_FALLBACK_SECONDS / 60.0
        return max(low, min(high, int(round(minutes * 60))))

    def _nap_recovery_per_minute(self, action: Any) -> float:
        """小睡动作配的「每分钟恢复多少精力」（配成设值 / 倍数时返回 0，用兜底时长）。"""

        effects = getattr(getattr(action, "on_complete", None), "effects_per_minute", None) or {}
        raw = str(effects.get("energy") or "").strip()
        try:
            rate = float(raw) if raw.startswith(("+", "-")) else 0
            if action is not None and (action.recovery_kind == "auto" or "energy" not in effects):
                rate += self.world.state_dynamics.nap_energy_recovery_per_min
            return max(0.0, rate)
        except ValueError:
            return 0.0

    # ---------------- 是否需要新计划 ----------------

    def needs_plan(self, state: WorldState) -> bool:
        active = active_plan(state)
        if active is not None:
            return False
        if state.current_action:
            return False
        return True

    def llm_sample_rate(self, state: WorldState) -> float:
        """问大模型的概率：在她「决策意愿」的 0~1 之间，按配置的上下限线性插值。

        意愿本身就来自那几个数值（孤独/无聊/好奇/心潮，再扣掉疲惫和被冷落），
        所以她是"闲得慌"还是"刚忙完很平静"，落到这里的概率是不一样的。
        """

        config = self.world.decider
        low = float(config.llm_rate_min)
        high = float(config.llm_rate_max)
        if low > high:
            low, high = high, low
        low = max(0.0, min(1.0, low))
        high = max(0.0, min(1.0, high))
        willingness = reply_willingness(state)
        return max(0.0, min(1.0, low + (high - low) * willingness))

    def should_ask_llm(self, state: WorldState) -> bool:
        """这一次评估要不要交给大模型安排。

        规则决策不受这个概率影响——规则命中就直接执行；这里决定的是
        「要不要额外问一次大模型」。没被抽中、规则又给不出计划时，这一轮她就自己待着。
        """

        return self.rng.random() < self.llm_sample_rate(state)

    # ---------------- 规则决策 ----------------

    def rule_plan(
        self,
        state: WorldState,
        *,
        group_chatting: bool = False,
        interject_allowed: bool = True,
    ) -> dict[str, Any] | None:
        """按优先级给出一段计划；无法判断时返回 None。

        ``group_chatting``：群里最近是否有人在聊（由引擎提供）。
        ``interject_allowed``：插话冷却与上限是否允许（由引擎提供）。

        「想被碰一碰」不在这里：那条要交给她自己决定（去哪、找谁、做什么），
        走引擎的 :meth:`MyWorldEngine._maybe_desire_push`。
        """

        node_id = state.node_id
        graph = self.world.adjacent()
        home = self.world.default_node_id()

        if self.world.sleep.mode == "activity" and recovery_needed(state.energy, self.world.sleep):
            recovery = self.recovery_plan(state)
            if recovery:
                return recovery

        # 0) 群里正热闹 -> 主动插一句。两条动机分开算：
        #    A「想被注意到」：孤独感够了、心情也不太差才愿意主动social；
        #    B「想发作」：情绪上来了、心情又差，会憋不住呛一句（形态由风格格决定）。
        #    孤独感管"想不想说话"，心潮/效价管"说成什么样"。
        if self.world.decider.enabled and interject_allowed and group_chatting:
            motive = self.interject_motive(state)
            if motive:
                plan = create_plan(
                    steps=[{"action": "say", "interject": True}],
                    world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                    valid_for=min(600, self._plan_valid()),
                    reason=motive,
                    source="rule",
                )
                if self._valid_candidate(state, plan):
                    return plan

        # 1) 精力过低 -> 该休息了（刚被叫醒的保护期里不安排，免得叫醒几分钟又被抓回去睡）。
        #    夜里睡整觉，白天先小睡：白天一睡八小时，醒来正好是半夜，日子直接过乱。
        #    除了精力，困意（sleep_pressure）攒够了也算——熬夜、被打断、被留住都会攒困意，
        #    但它不像"到点就困"那样天一黑就翻脸。
        #    「等会儿再睡」的延迟窗口里两条路都不走：她答应了，就真的再待一会儿。
        exhausted = state.energy < 0.25
        sleepy = self.sleep_pressure_high(state)
        if (
            (exhausted or sleepy)
            and (self.world.sleep.mode == "fixed" or (not self.is_night() and self.can_nap(state)))
            and state.world_time >= state.no_sleep_until
            and not state.sleep_delayed
            and (self.can_nap(state) or self.world.sleep_allowed_now(self.local_hour()))
        ):
            steps = self._rest_steps(state, "sleep" if self.is_night() or not self.can_nap(state) else "nap")
            if self.is_night() or not self.can_nap(state):
                reason = "困得撑不住了，该睡了" if sleepy and not exhausted else "精力低，该休息了"
            else:
                reason = (
                    "白天困得撑不住了，先眯一会儿"
                    if sleepy and not exhausted
                    else "白天精力低，先小睡一会儿"
                )
            plan = create_plan(
                steps=steps,
                world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                valid_for=self._plan_valid(),
                reason=reason,
                source="rule",
            )
            if self._valid_candidate(state, plan):
                return plan

        # 2~5) 剩下的几件事都**不是**非做不可的：各自先判断"够不够格"，
        #      再由今天的基调给它们拨权重，从够格的里面挑一件。
        #      （精力那条是安全阀，按原样强制，不参与加权。）
        candidates: list[tuple[str, dict[str, Any]]] = []
        lived = self._custom_life_plan(state, grounded_only=True)
        if lived:
            candidates.append(("everyday", lived))

        # （原来这里有一条"孤独 → 走到大厅喊一声"。删掉了：
        #   她说话是发到会话里的，跟站在哪个房间无关，走那几步纯属浪费；
        #   而"想找人"这件事本来就该由「想念 → 主动去找他」那条路负责
        #   （见引擎的 `_maybe_miss_push`）。孤独感会让她涨得更想他——
        #   见 `branch_weights` 里 reach_out 那一档，现在用来抬主动找他的意愿。）

        # 3) 好奇心高且在书房 -> 上网搜索并分享
        if (
            state.curiosity > 0.7
            and node_id == "study"
            and self.action_usable("search_web")
            and not self.searched_recently(state)
        ):
            step: dict[str, Any] = {"action": "search_web", "params": {}}
            topic = self._search_topic()
            if topic:
                # 规则触发也要说清"查什么"：用户给动作配过主题就直接用，
                # 没配就留空，交给引擎用查询模板 / 中性兜底主题
                step["intent"] = topic
            candidates.append(
                (
                    "search",
                    create_plan(
                        steps=[step],
                        world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                        valid_for=self._plan_valid(),
                        reason="好奇心高，想查点东西",
                        source="rule",
                    ),
                )
            )

        # 心情低落时，自我调节类动作的门槛下调：她更容易选择"缓一缓"，
        # 而不是等着谁来看穿她（安全阀是行为倾向，不是触发一个动作）。
        low_mood = float(state.valence) < SELF_CARE_VALENCE

        # 4) 无聊 -> 换个地方发呆
        wander_eligible = state.boredom > (SELF_CARE_BOREDOM if low_mood else 0.8)
        if wander_eligible:
            target = self._pick_idle_node(exclude=node_id)
            steps = self._travel_then(node_id, target, graph) if target else []
            steps.append({"action": "stare"})
            candidates.append(
                (
                    "wander",
                    create_plan(
                        steps=steps,
                        world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                        valid_for=self._plan_valid(),
                        reason="太无聊了，换个环境",
                        source="rule",
                    ),
                )
            )

        # 5) 在家待腻了 -> 出门走走。
        #    没有这一条她可以连着几天不出门：规则里"找人就近去客厅"、"看书去书房"，
        #    全是屋里；发呆那条虽然可能抽到公园 / 海边，但门槛高、还老被别的动机挤掉。
        #    这里只管"在家待腻了"这一种：好奇心那条走的是上网查，不是出门。
        if (
            self.world.is_home_zone(node_id)
            and not self.is_night()
            and not wander_eligible
            and state.energy > OUTDOOR_MIN_ENERGY
            and state.boredom > OUTDOOR_MIN_BOREDOM
        ):
            target, doing = self._pick_outdoor_node(state)
            steps = self._travel_then(node_id, target, graph) if target else []
            if doing:
                steps.append({"action": doing})
            candidates.append(
                (
                    "outdoor",
                    create_plan(
                        steps=steps,
                        world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                        valid_for=self._plan_valid(),
                        reason="在家待久了，想出去走走",
                        source="rule",
                    ),
                )
            )

        # 6) 精力尚可且是白天 -> 去书房看书
        if (
            state.energy > 0.5
            and state.boredom > (SELF_CARE_IDLE if low_mood else 0.45)
            and node_id != "study"
        ):
            steps = self._travel_then(node_id, "study", graph)
            steps.append({"action": "read", "duration": self._action_duration("read")})
            candidates.append(
                (
                    "read",
                    create_plan(
                        steps=steps,
                        world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                        valid_for=self._plan_valid(),
                        reason="有点闲，去看会儿书",
                        source="rule",
                    ),
                )
            )

        return self._pick_branch(state, candidates)

    def branch_weights(self, state: WorldState) -> dict[str, float]:
        """今天各决策分支的权重（1.0 = 跟平常一样）。

        两个来源：今天的基调（懒散的日子不太想查东西、黏人的日子特别想找人）、
        以及欲求（想被摸摸的时候更愿意往人那边凑）。

        ``reach_out`` 这一档**不再对应规则里的一条分支**（那条"走去大厅喊一声"
        已经删了）：它现在是"她有多想主动去找他"的倍率，由引擎在
        `_maybe_miss_push` 里读着用——黏人的日子、想被碰的时候，她更容易主动开口。
        """

        weights = {name: 1.0 for name in DAY_BRANCH_KEYS}
        if bool(getattr(self.world.state_dynamics, "daily_mood_enabled", True)):
            try:
                strength = float(
                    getattr(self.world.state_dynamics, "daily_mood_strength", 1.0)
                )
            except (TypeError, ValueError):
                strength = 1.0
            weights.update(
                day_mood_branches(
                    str(getattr(state, "day_mood", "") or ""), strength=strength
                )
            )
        if float(getattr(state, "desire", 0.0) or 0.0) > DESIRE_HUNGRY:
            # 想被碰一碰的时候，主动找他的意愿抬起来（她想往人那边凑）
            weights["reach_out"] *= 1.6
        return weights

    def reach_out_eagerness(self, state: WorldState) -> float:
        """她这会儿有多想主动找他（1.0 = 平常）。引擎拿它压/抬"想念"的门槛。"""

        try:
            value = float(self.branch_weights(state).get("reach_out", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return max(0.2, min(4.0, value))

    def _pick_branch(
        self,
        state: WorldState,
        candidates: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """从几个都够格的分支里挑一个。

        权重全一样（平常的日子）就按原来的优先级走，不掷骰子——决策要可复现，
        只有今天确实偏了才让它显出"今天跟昨天不一样"。
        """

        candidates = [(name, plan) for name, plan in candidates if self._valid_candidate(state, plan)]
        if not candidates:
            return self._custom_life_plan(state)
        if len(candidates) == 1:
            return candidates[0][1]
        weights = self.branch_weights(state)
        scaled = [max(0.0, float(weights.get(name, 1.0))) for name, _plan in candidates]
        if len({round(item, 6) for item in scaled}) == 1:
            return candidates[0][1]
        total = sum(scaled)
        if total <= 0:
            return candidates[0][1]
        point = self.rng.random() * total
        acc = 0.0
        for (_name, plan), weight in zip(candidates, scaled):
            acc += weight
            if point < acc:
                return plan
        return candidates[-1][1]

    def _valid_candidate(self, state, plan) -> bool:
        if not plan or not plan.get("steps"):
            return False
        position = state.node_id
        for step in plan["steps"]:
            action = self.world.action_map().get(str(step.get("action") or ""))
            if not action or not action.enabled or action.idle:
                return False
            from .activity_time import window_allows
            if not window_allows(action.preconditions.time_window, self.world, self.local_hour()):
                return False
            if self.action_check and not self.action_check(state, action):
                return False
            if (action.preconditions.min_energy is not None and state.energy < action.preconditions.min_energy
                    or state.state in action.preconditions.not_state):
                return False
            if action.id == "walk_to":
                target = str(step.get("target_node") or "")
                if target not in self.world.node_map() or find_path(self.world.adjacent(), position, target) is None:
                    return False
                position = target
            elif not action.available_in(position):
                return False
        return True

    def _custom_life_plan(self, state, *, grounded_only: bool = False):
        from .life import life_role
        if state.energy < .4 or state.boredom <= .45:
            return None
        from .life_matters import practice_records, routine_hints
        persona_key = self.persona_key_provider(state) if self.persona_key_provider else None
        hour = self.local_hour()
        period = "早晨" if 5 <= hour < 11 else "白天" if 11 <= hour < 18 else "晚间" if 18 <= hour < 23 else "夜间"
        preferred = {item["action"]: item for item in routine_hints(state, now=self._now(), persona_key=persona_key) if item["period"] == period}
        recent = {key: row for key, row in practice_records(state, persona_key=persona_key).items()
                  if 0 <= self._now() - float(row.get("last_at") or 0) < 90 * 60}
        choices = []
        graph = self.world.adjacent()
        nodes = sorted(self.world.nodes, key=lambda node: node.id != state.node_id)
        for action in self.world.actions:
            if (not action.enabled or action.idle or action.llm_level != "template"
                    or action.target_type != "none" or life_role(action) not in {"interest", "explore"}
                    or action.tool_list() or action.trigger_command or action.on_complete.trigger not in {"none", "llm_followup"}):
                continue
            if grounded_only and (action.id in recent or (action.id not in preferred and not recent)):
                continue
            for node in nodes:
                if not action.available_in(node.id):
                    continue
                if node.id != state.node_id and not self.action_usable("walk_to"):
                    continue
                if find_path(graph, state.node_id, node.id) is None:
                    continue
                score = (0.2 if node.id == state.node_id else 0) + min(2, preferred.get(action.id, {}).get("weight", 0))
                if self.world.night.quiet_life and self.is_night():
                    score += .5 if self.world.is_home_zone(node.id) else -2
                    if life_role(action) == "explore":
                        score -= 2  # An outdoor habit alone does not make every night an outing.
                reason = f"有点空闲，想{action.name}"
                if action.id in recent:
                    score -= 2
                elif recent:
                    last = max(recent.values(), key=lambda row: row.get("last_at", 0))
                    reason = f"刚才已投入一段时间{last['label']}，现在换成{action.name}，缓一缓"
                if action.id in preferred and action.id not in recent:
                    reason = f"{preferred[action.id]['explanation']}；现在条件合适，接着{action.name}"
                plan = create_plan(steps=[*self._travel_then(state.node_id, node.id, graph),
                                          {"action": action.id, "duration": self._action_duration(action.id)}],
                                   world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                                   valid_for=self._plan_valid(), reason=reason, source="rule")
                if self._valid_candidate(state, plan):
                    choices.append((score, plan))
                    break
        if not choices:
            return None
        return max(choices, key=lambda row: row[0])[1]

    def forced_plan(self, state: WorldState, flag: str) -> dict[str, Any] | None:
        """极端保护触发的计划。"""

        graph = self.world.adjacent()
        home = self.world.default_node_id()
        if flag == "force_sleep":
            if self.world.sleep.mode == "activity":
                return self.recovery_plan(state)
            if not self.can_nap(state) and not self.world.sleep_allowed_now(self.local_hour()):
                return None
            steps = self._rest_steps(state, "sleep" if self.is_night() or not self.can_nap(state) else "nap")
            # 白天透支不该一睡八小时：先眯到小睡动作的上限，缓过来再说过。
            if self.is_night() or not self.can_nap(state):
                reason = "精力透支，强制休息"
            else:
                reason = "精力透支，先眯一会儿"
            plan = create_plan(
                steps=steps,
                world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                valid_for=self._plan_valid(),
                reason=reason,
                source="extreme",
            )
            return plan if self._valid_candidate(state, plan) else None
        if flag == "force_reach_out":
            # 太久没和人说话了：原地开口就行（说话发到会话里，跟房间无关）
            steps = [{"action": "say"}]
            plan = create_plan(
                steps=steps,
                world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                valid_for=self._plan_valid(),
                reason="太久没和人说话了",
                source="extreme",
            )
            return plan if self._valid_candidate(state, plan) else None
        if flag == "need_change":
            target = self._pick_idle_node(exclude=state.node_id)
            if target:
                steps = self._travel_then(state.node_id, target, graph)
                steps.append({"action": "stare"})
                plan = create_plan(
                    steps=steps,
                    world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                    valid_for=self._plan_valid(),
                    reason="想换换环境",
                    source="extreme",
                )
                return plan if self._valid_candidate(state, plan) else self._custom_life_plan(state)
        return None

    # ---------------- 工具 ----------------

    def _rest_steps(self, state, kind: str) -> list[dict[str, Any]]:
        from .pathfinding import nearest_node
        graph = self.world.adjacent()
        for action in self._rest_candidates(kind):
            if kind == "nap" and self._nap_recovery_per_minute(action) <= 0:
                continue
            candidates = [node.id for node in self.world.nodes if action.available_in(node.id)]
            if action.id == "sleep" and self.world.sleep_node_id():
                candidates = [self.world.sleep_node_id()]
            target = nearest_node(graph, state.node_id, candidates)
            if not target:
                continue
            duration = self.nap_seconds(state, action) if kind == "nap" else self._action_duration(action.id)
            steps = [*self._travel_then(state.node_id, target, graph), {"action": action.id, "duration": duration}]
            if self._valid_candidate(state, {"steps": steps}):
                return steps
        return []

    def _travel_then(
        self, current: str, target: str, graph: dict[str, list[tuple[str, int]]]
    ) -> list[dict[str, Any]]:
        if not target or current == target:
            return []
        if find_path(graph, current, target) is None:
            return []
        return [{"action": "walk_to", "target_node": target}]

    def _pick_idle_node(self, *, exclude: str = "") -> str:
        """挑一个能发呆的地方：动作归属只认动作那一份（不再看节点自己的列表）。"""

        candidates = [
            node.id
            for node in self.world.nodes
            if node.id != exclude
            and any(action.id == "stare" for action in self.world.actions_in(node.id))
        ]
        if not candidates:
            candidates = [n.id for n in self.world.nodes if n.id != exclude]
        if not candidates:
            return ""
        return self.rng.choice(candidates)

    def _pick_outdoor_node(self, state: WorldState) -> tuple[str, str]:
        """出门该去哪、到了做什么：返回 ``(地点 id, 动作 id)``。

        只在家外（``zone != 首页区``）的地点里选，优先"到了真有事可做"的
        （商场 / 小吃街 / 图书馆 / 教室 / 操场…），纯发呆的地方排后面；
        好奇心不低于无聊时，从最热闹的那一半里挑。
        """

        home_zone = self.world.home_zone_id()
        rows: list[tuple[float, str, str]] = []
        for node in self.world.nodes:
            zone = self.world.zone_of(node.id)
            if zone == home_zone or find_path(self.world.adjacent(), state.node_id, node.id) is None:
                continue
            actions = [
                action
                for action in self.world.actions_in(node.id)
                if action.scope == "node" and action.enabled and not action.idle and not is_sleep_action(action)
            ]
            if not actions:
                continue
            # 到了先做点什么：优先"正经事"（逛街 / 吃点东西 / 看会儿书），
            # 没有就发呆——别挑一个跟目的地对不上的动作，那样到点会被跳过。
            doing = next(
                (action.id for action in actions if action.id != "stare"), actions[0].id
            )
            lively = float(len([action for action in actions if action.id != "stare"]))
            rows.append((lively, node.id, doing))
        if not rows:
            # 外面一个能去的都没有：退回"有发呆动作的地方"，再不行就哪儿都不去
            rows = []
        if not rows:
            return "", ""
        if float(state.curiosity) >= float(state.boredom):
            rows.sort(key=lambda item: (-item[0], item[1]))
            pool = rows[: max(1, len(rows) // 2)]
        else:
            pool = rows
        _lively, node_id, doing = self.rng.choice(pool)
        return node_id, doing

    def _action_duration(self, action_id: str) -> int:
        action = self.world.action_map().get(action_id)
        return int(action.duration) if action else 0

    def _plan_valid(self) -> int:
        return int(self.world.limits.plan_valid_duration)

    def has_tool(self, name: str) -> bool:
        """她能不能用到这个工具：绑了它的动作存在，或者它是全局通用工具。"""

        wanted = str(name or "").strip()
        if not wanted:
            return False
        if wanted in {str(item).strip() for item in (self.world.global_allowed_tools or [])}:
            return True
        return any(
            action.llm_level == "tool" and wanted in action.tool_list()
            for action in self.world.actions
        )

    def action_usable(self, action_id: str, *, check_time: bool = True) -> bool:
        """这个动作现在真的跑得起来吗（存在、没停用、需要工具时至少配了一个）。"""

        action = self.world.action_map().get(str(action_id or "").strip())
        from .activity_time import window_allows
        if check_time and action and not window_allows(action.preconditions.time_window, self.world, self.local_hour()):
            return False
        if action is None or not action.enabled or action.idle:
            return False
        if action.llm_level == "tool":
            return bool(action.tool_list())
        return True

    def _search_topic(self, action_id: str = "search_web") -> str:
        """这个检索动作配好的固定主题（没配就返回空串，由引擎那边兜底）。"""

        action = self.world.action_map().get(action_id)
        return str(getattr(action, "search_topic", "") or "").strip() if action else ""

    def searched_recently(self, state: WorldState, action_id: str = "search_web") -> bool:
        """她刚查过没有：查一次只降一点点好奇心，不拦一下会变成每隔几分钟查同一件事。"""

        cooldown = SEARCH_COOLDOWN_MINUTES * 60.0 / max(1.0, self.tick_seconds)
        if cooldown <= 0:
            return False
        now = int(state.world_time or 0)
        for item in reversed(list(getattr(state, "recent_events", []) or [])):
            detail = item.get("detail") if isinstance(item, dict) else None
            if not isinstance(detail, dict):
                continue
            name = str(detail.get("action") or detail.get("type") or "")
            if name != action_id:
                continue
            when = int(item.get("world_time") or 0)
            if now - when <= cooldown:
                return True
        return False

    # ---------------- 插话 ----------------

    def interject_threshold(self, state: WorldState) -> float:
        """这一刻要多少孤独感才愿意主动接话。

        **浮动阈值**（不是浮动意愿值）：心情差的时候更难开口，但一旦开口，
        说成什么样仍归风格格管——两件事不混在一起。

        心情好时反过来：阈值往下走一点（最多 ``HAPPY_THRESHOLD_DROP``）——
        "高心潮 + 高效价"才该变成想找人玩，而不是只让她说话更外放。
        """

        base = float(self.world.decider.interject_threshold)
        valence = float(state.valence)
        if valence < 0.5:
            bump = max(
                0.0,
                min(INTERJECT_THRESHOLD_BUMP_MAX, (0.5 - valence) / 3.0),
            )
        else:
            bump = -HAPPY_THRESHOLD_DROP * min(1.0, (valence - 0.5) / 0.4)
        if self.motive_a_closed(state):
            # 长期低落：动机 A 关闭，而且至少关 15 分钟（避免阈值边上反复抖）
            bump = INTERJECT_THRESHOLD_BUMP_CLOSED
        return max(0.0, base + bump)

    def motive_a_closed(self, state: WorldState) -> bool:
        """「想被注意到」这条动机是不是被关掉了。"""

        return self._now() < float(state.interject_closed_until or 0.0)

    def interject_motive(self, state: WorldState) -> str:
        """她想插话的理由；不想插话时返回空字符串。"""

        valence = float(state.valence)
        if valence > INTERJECT_SOCIAL_VALENCE_FLOOR and not self.motive_a_closed(state):
            if state.loneliness >= self.interject_threshold(state):
                return "群里正聊得热闹，想接一句"
        if valence < INTERJECT_SOCIAL_VALENCE_FLOOR and float(state.affect) >= INTERJECT_VENT_AROUSAL:
            return "情绪上来了，忍不住想插一句"
        return ""
