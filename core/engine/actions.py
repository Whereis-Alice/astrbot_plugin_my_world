"""动作执行与工具熔断。"""

from __future__ import annotations

from ..model_calls import model_operation

import asyncio
import math
from typing import Any
from ..json_actions import (
    PlannedAction,
    extract_json_object,
)
from ..models import (
    ActionDef,
    NodeDef,
)
from ..pathfinding import find_path, nearest_node
from ..planner import (
    active_plan,
    advance,
    create_plan,
    same_plan,
)
from ..speech_policy import speech_origin
from ..state import (
    STATE_DROWSY,
    WorldState,
)
from ..tool_policy import is_self_send_tool
from .models import TickOutcome
from .util import (
    _clip_text,
    _looks_like_tool_broken,
    _to_int_or_default,
    render_param_text,
)


class ActionsMixin:
    def activity_view(self, state: WorldState) -> dict[str, Any]:
        from ..life import activity_view

        return activity_view(state, self.world)

    def _apply_event_life(self, state: WorldState, tier: str, explicit: dict[str, Any]) -> None:
        from ..life import event_relief

        config = self.world.state_dynamics
        if not config.independent_life_enabled or tier == "skip":
            return
        effects = event_relief(tier, explicit)
        self.dynamics.apply_pulse(state, {key: value * config.life_relief_strength
                                        for key, value in effects.items()}, now=self._now())

    def _tick_idle_life(self, state: WorldState) -> None:
        """Run a quiet background action; formal work always has priority."""
        from ..life import idle_definition, safe_idle_action

        if not self.world.idle_life.enabled or state.current_action or state.state != "idle":
            state.idle_activity = {}
            return
        if idle_definition(state, self.world):
            return
        # Only a naturally completed idle action gets its configured completion effects.
        previous = state.idle_activity or {}
        old = self.world.action_map().get(str(previous.get("id") or ""))
        if (old and safe_idle_action(old) and old.available_in(state.node_id) and previous.get("node_id") == state.node_id
                and 0 < int(previous.get("until_tick") or 0) <= state.world_time):
            self.dynamics.apply_effects(state, old.on_complete.effects, world=self.world, now=self._now())
            minutes = max(0, int(previous.get("until_tick", 0)) - int(previous.get("start_tick", 0))) * self.tick_seconds / 60
            self.dynamics.apply_effects(state, old.on_complete.effects_per_minute,
                                       world=self.world, scale=minutes, now=self._now())
        candidates = [action for action in self.world.actions if (
            safe_idle_action(action) and action.available_in(state.node_id)
            and self.action_quota_left(state, action) > 0
            and self._check_preconditions(state, action)[0]
        )]
        fresh = [action for action in candidates if action.id != previous.get("id")]
        state.idle_activity = {}
        if not candidates:
            return
        definition = self.rng.choice(fresh or candidates)
        self._count_action_use(state, definition)
        config = self.world.idle_life
        low = min(config.duration_min_minutes, config.duration_max_minutes)
        high = max(config.duration_min_minutes, config.duration_max_minutes)
        seconds = definition.duration if definition.duration_mode == "fixed" and definition.duration else self.rng.randint(low, high) * 60
        seconds = max(low * 60, min(high * 60, seconds))
        ticks = max(1, math.ceil(seconds / max(1, self.tick_seconds)))
        state.idle_activity = {"id": definition.id, "node_id": state.node_id,
                               "start_tick": state.world_time, "until_tick": state.world_time + ticks}

    def _apply_life_completion(self, state: WorldState, definition: ActionDef | None,
                               *, elapsed_minutes: float = 0) -> None:
        from ..life import completion_relief

        if definition is None or not self.world.state_dynamics.independent_life_enabled:
            return
        effects = completion_relief(definition, elapsed_minutes=elapsed_minutes)
        self.dynamics.apply_pulse(state, {key: value * self.world.state_dynamics.life_relief_strength
                                        for key, value in effects.items()}, now=self._now())

    # ================= 动作执行 =================

    def _available_action_ids(self, node_id: str) -> set[str]:
        return {
            action.id
            for action in self.world.actions_in(node_id)
            if self._action_usable_now(action)
        }

    def _usable_action_ids(self, state: WorldState, node_id: str) -> set[str]:
        """这一轮真的能用的动作：地点 + 时段 + 没配额用尽。"""

        blocked = self.exhausted_actions(state)
        return {
            action.id for action in self.world.actions_in(node_id) if action.id not in blocked
        }

    def _action_usable_now(self, action: Any) -> bool:
        """这个动作现在这个时段能不能用。

        目前只有一条：非夜晚不给「睡觉」（提示词不提供、解析也拒掉）。
        计划与日程实际执行时也检查此限制。
        """

        if str(getattr(action, "id", "")) != "sleep":
            return not bool(getattr(action, "idle", False))
        return self.world.sleep_allowed_now(self.local_now().hour)

    def _nearest_node_with_action(self, node_id: str, definition: ActionDef) -> str:
        """哪个可达地点能做这个动作（挑最近的）。"""

        candidates = [
            node.id for node in self.world.nodes if definition.available_in(node.id)
        ]
        if definition.id == "sleep" and self.world.sleep_node_id():
            candidates = [self.world.sleep_node_id()]
        if not candidates:
            return ""
        return nearest_node(self.world.adjacent(), node_id, candidates) or ""

    def _parseable_action_ids(
        self, node_id: str, *, blocked: set[str] | None = None
    ) -> set[str]:
        """解析模型输出时认可的动作集合。

        默认 = 当前地点能做的动作。开启「允许她想去别处做某事」后，
        可达地点能做的动作也算数——她写出来，插件负责带她过去。
        ``blocked`` 是这一轮要挡掉的（配额用尽那批）：提示词里不列、解析也拒。
        """

        if not bool(self.world.remote_action_travel):
            ids = self._available_action_ids(node_id)
            return {item for item in ids if item not in (blocked or set())}
        ids = {
            action.id
            for action in self.world.actions
            if self._action_usable_now(action)
            and (
                action.available_in(node_id)
                or self._nearest_node_with_action(node_id, action)
            )
        }
        return {item for item in ids if item not in (blocked or set())}

    @staticmethod
    def _plan_has_pending_step(state: WorldState) -> bool:
        """这一步是否还有安排（计划里还有没做的步骤）。

        只看、不动状态：``peek_step`` 会在计划结束时顺手清掉计划，
        这里只想知道"落地之后是不是还有事要做"。
        """

        plan = active_plan(state)
        if not isinstance(plan, dict):
            return False
        steps = plan.get("steps") or []
        return int(plan.get("current_step", 0)) < len(steps)

    def _step_to_action(self, step: dict[str, Any]) -> PlannedAction:
        return PlannedAction(
            type=str(step.get("action", "")),
            interject=bool(step.get("interject", False)),
            messages=list(step.get("messages") or []),
            target=str(step.get("target", "") or ""),
            target_node=str(step.get("target_node", "") or ""),
            content=str(step.get("content", "") or ""),
            intent=str(step.get("intent", "") or ""),
            params=dict(step.get("params") or {}),
            duration=int(step.get("duration", 0) or 0),
            queries=[str(item) for item in (step.get("queries") or []) if str(item)],
            search_depth=str(step.get("search_depth", "") or ""),
            read_pages=_to_int_or_default(step.get("read_pages"), -1),
            send_to=str(step.get("send_to", "") or ""),
            raw=step,
        )

    def _speech_target(
        self, state: WorldState, action: PlannedAction, outcome: TickOutcome
    ) -> str | None:
        """这一步说给哪个会话听。

        - 她没写落点：跟着这一轮的落点走；
        - 写了、而且对得上：发到那个会话；
        - 写了、但对不上（例如「私聊某个不在名单里的人」）：``None``，
          这句话干脆不说——总比把她想私下说的话发到群里强。
        - **日程锁**（``outcome.locked_targets``）：这一轮是用户排的日程，落点已经
          定死在勾选的会话（勾组 = 组里全部成员）。她写的 ``send_to`` 只要越出这个
          集合就不发——不然"只勾了一个私聊"的日程会被模型那句 ``send_to`` 改发到
          没勾的会话去（单目标时 ``broadcast_targets`` 是空的，光看它拦不住）。
          非日程路径不会设这个锁，跨会话 ``send_to``（"去群里说声晚安"）照旧有效。
        """

        raw = str(getattr(action, "send_to", "") or "").strip()
        if not raw:
            # 没写落点：她"正在说的那个会话"优先（动作续说、亲昵动作都跟着它走）
            return outcome.home()
        target = self.resolve_send_to(state, raw, fallback="")
        if not target:
            return None
        allowed = {str(outcome.home()), *(str(item) for item in (outcome.broadcast_targets or []))}
        locked = {
            str(item)
            for item in (getattr(outcome, "locked_targets", None) or [])
            if str(item).strip()
        }
        if locked and target not in locked:
            outcome.notes.append(f"日程落点限制：忽略未选中的 send_to {target}")
            return None
        if outcome.broadcast_targets and not locked and target not in allowed:
            outcome.notes.append(f"日程落点限制：忽略未选中的 send_to {target}")
            return None
        return target

    def _plan_target(
        self,
        state: WorldState,
        outcome: TickOutcome,
        plan: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        """计划级的落点：她写了就用她的，写不出来就留在当前会话（并记一笔）。"""

        raw = str((plan or {}).get("send_to") or "").strip()
        if not raw:
            return str(fallback)
        target = self.resolve_send_to(state, raw, fallback="")
        if target:
            return target
        outcome.notes.append(
            f"落点「{raw}」不在她能说话的地方里，这一轮按当前会话处理"
        )
        return str(fallback)

    def _freeze_plan_target(
        self,
        state: WorldState,
        outcome: TickOutcome,
        plan: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        """把"这份计划要在哪儿说"当场定死，写进计划和每一步。

        不定死的话，落点是**执行那一刻**按"谁在推进这一拍"算的：一个会话组里几个
        群 / 私聊共用同一份状态，她在私聊里决定要说的话，可能被群里那一拍捡去执行，
        说出口的地方就跟着变了（日志写着私聊、消息却出现在群里）。
        每一步上的 ``session`` 就是执行时用的落点，``send_to`` 留给人看。
        """

        target = self._plan_target(state, outcome, plan, fallback=fallback)
        plan["send_to"] = str(target or "")
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            raw = str(step.get("send_to") or "").strip()
            if raw:
                # 她自己给某一步单独写了落点（"先去群里说，再私聊告诉他"）
                picked = self.resolve_send_to(state, raw, fallback="")
                if not picked:
                    outcome.notes.append(
                        f"这一步想说到「{raw}」，但那儿不在她能说话的地方里，"
                        "改按整份计划的落点处理"
                    )
                    step["send_to"] = ""
                    picked = str(target or "")
            else:
                picked = str(target or "")
            if not str(step.get("session") or "").strip():
                step["session"] = picked
        return str(target or "")

    def talking_session(self, state: WorldState) -> str:
        """最近别人是在哪个会话里说话的。

        她想接话、想找人聊的时候，落点看这个：会话组里几个群 / 私聊共用一份状态，
        「群里正热闹」里的"群里"是**那个真的有人在说话的会话**，不是碰巧在推进的这一拍。
        """

        for item in reversed(self.chat_window(state)):
            if item.get("is_self"):
                continue
            origin = str(item.get("origin") or "")
            if origin and self.is_enabled(origin):
                return origin
        return str(state.session_id)

    def _plan_home_for(
        self, state: WorldState, plan: dict[str, Any], fallback: str
    ) -> str:
        """这份计划没写落点时该落在哪儿。

        规则决策里"她主动开口"的动机（群里热闹想接一句、孤独了想找人说话）本来就来自
        某个人在某个会话里说的话，那就落在那个会话；其余情况留在决定它的地方。
        """

        steps = [step for step in (plan.get("steps") or []) if isinstance(step, dict)]
        if not steps:
            return str(fallback)
        wants_people = any(step.get("interject") for step in steps)
        if not wants_people and str(plan.get("source") or "") == "rule":
            # 规则排的"去大厅找人说话"这类计划：没有 interject 标记，但同样是冲着人去的
            wants_people = any(
                str(step.get("action") or "") == "say" for step in steps
            )
        if not wants_people:
            return str(fallback)
        return self.talking_session(state) or str(fallback)

    @staticmethod
    def _step_payload(
        item: PlannedAction, session: str = "", kind: str = ""
    ) -> dict[str, Any]:
        """把待执行动作转成计划里的一步。

        注意 ``intent`` 一定要带上：工具型动作的参数就是靠它补出来的，
        少了它这一步到点执行时只会得到「没有给出想做什么」。

        ``kind`` 记的是这一步的来路（``reply`` = 当时有人在跟她说话）：
        排队到后面才轮到时，这种话不该被"主动发言冷却"吞掉。
        """

        return {
            "action": item.type,
            "kind": str(kind or ""),
            "interject": bool(item.interject),
            "messages": list(item.messages or []),
            "target": item.target,
            "target_node": item.target_node,
            "duration": item.duration,
            "content": item.content,
            "intent": item.intent,
            "params": dict(item.params or {}),
            "queries": list(item.queries or []),
            "search_depth": str(item.search_depth or ""),
            "read_pages": int(item.read_pages),
            "send_to": str(item.send_to or ""),
            # 这一步是"在哪儿安排的"：轮到她做的时候，说话回那儿
            "session": str(session or ""),
            "status": "pending",
        }

    @staticmethod
    def _plan_remaining_steps(
        plan: dict[str, Any] | None,
        *,
        skip_current: bool = False,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        """一份计划里还没做完的步骤。

        ``skip_current=True`` 用于"正在执行计划里这一步"的场景：那一步已经拿在手上了，
        再排一遍会重复执行。
        ``after`` 直接指定从哪一步之后开始算：她自己那一步正在跑时必须用它，
        光看 ``current_step`` 已经不准了（中途可能往队里插过新的动作）。
        """

        if not isinstance(plan, dict):
            return []
        steps = plan.get("steps") or []
        if after is None:
            index = max(0, int(plan.get("current_step", 0) or 0))
            if skip_current:
                index += 1
        else:
            index = max(0, int(after) + 1)
        return [
            dict(step)
            for step in steps[index:]
            if isinstance(step, dict) and step.get("action")
        ]

    def _running_plan_index(
        self, state: WorldState, plan: dict[str, Any] | None
    ) -> int | None:
        """她手上正在做的那一步在这份计划里的下标。

        计划中途可能被插进新的安排（``current_step`` 会指到别的地方），所以不能只看
        下标；这里要求动作是**从这份计划**起跑的，计划号和下标都对得上才算数。
        """

        if not isinstance(plan, dict):
            return None
        action = state.current_action if isinstance(state.current_action, dict) else None
        if not action or not action.get("from_plan"):
            return None
        recorded = action.get("plan_created_at")
        if recorded is None and not action.get("plan_id"):
            return None
        try:
            if not same_plan(action, plan):
                return None
            index = int(action.get("plan_index") or 0)
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(plan.get("steps") or []):
            return index
        return None

    def _plan_queue_anchor(self, state: WorldState, plan: dict[str, Any]) -> int:
        """新排的动作插在计划的第几步：她手上那一步正跑就插在它后面，否则插在当前步上。

        插进去而不是"重建一份计划"，是为了让正在跑的那一步还认得出自己——
        重建会把它的下标冲掉，等它做完时一推进就会误伤排在前面的新动作。
        """

        steps = plan.get("steps") or []
        running = self._running_plan_index(state, plan)
        if running is not None:
            index = running + 1
        else:
            index = int(plan.get("current_step", 0) or 0)
        return max(0, min(index, len(steps)))

    def _queue_steps(
        self,
        state: WorldState,
        outcome: TickOutcome,
        rest: list[PlannedAction],
        carried: list[dict[str, Any]],
        *,
        kind: str,
        note: str,
    ) -> None:
        """把这一批里还没做的动作排进计划：接在她手头那一步之后，原来的安排照样保住。"""

        fresh = [self._step_payload(item, outcome.home(), kind) for item in rest]
        plan = state.current_plan if isinstance(state.current_plan, dict) else None
        # 「睡觉 / 小睡」排第二遍没有意义：临睡期里她每回一句都可能再排一次，
        # 计划里就会堆成"睡觉 ×4"（真到了点也只该睡一觉）。
        fresh, dropped = self._dedupe_queued_steps(
            fresh, list((plan or {}).get("steps") or []) + carried
        )
        if dropped:
            outcome.notes.append(f"「睡觉」这一类动作已经排过了，重复的 {dropped} 步丢掉")
        if plan is None:
            state.current_plan = create_plan(
                steps=fresh + carried,
                world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
                valid_for=self.world.limits.plan_valid_duration,
                reason="同一轮里还没做完的动作",
                source="pending",
            )
        else:
            anchor = self._plan_queue_anchor(state, plan)
            steps = list(plan.get("steps") or [])
            steps[anchor:anchor] = fresh
            plan["steps"] = steps
            # 新排的动作有自己的有效期，别一排队就过期
            plan["expires_at"] = max(float(plan.get("expires_at") or 0), self._now() + self.world.limits.plan_valid_duration)
            plan["valid_until"] = max(
                int(plan.get("valid_until") or 0),
                int(state.world_time) + max(1, math.ceil(self.world.limits.plan_valid_duration / self.tick_seconds)),
            )
        if note:
            outcome.notes.append(note)

    DEDUPE_ACTION_IDS = ("sleep", "nap")
    """这些动作"做一次就够了"：同一份计划里排第二遍没有意义。

    临睡期里她每回一句话就可能再排一次「睡觉」，计划很快就堆成"睡觉 ×4"。
    """

    def _dedupe_queued_steps(
        self,
        steps: list[dict[str, Any]],
        existing: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """把"排第二遍没意义"的动作去掉，返回 ``(留下的步骤, 丢掉几个)``。"""

        seen = {
            str(item.get("action") or "")
            for item in (existing or [])
            if isinstance(item, dict)
        }
        kept: list[dict[str, Any]] = []
        dropped = 0
        for step in steps:
            if not isinstance(step, dict):
                continue
            action_id = str(step.get("action") or "")
            if action_id in self.DEDUPE_ACTION_IDS:
                if action_id in seen:
                    dropped += 1
                    continue
                seen.add(action_id)
            kept.append(step)
        return kept, dropped

    @speech_origin("actions")
    async def _execute_actions(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        actions: list[PlannedAction],
        *,
        depth: int,
        autonomous: bool,
        from_plan: bool = False,
        allow_remote_travel: bool = True,
        from_schedule: bool = False,
        skip_drowsy: bool = False,
    ) -> bool:
        """执行一串动作。返回"有没有真的执行到至少一个"。

        ``allow_remote_travel=False`` 时不做「她想去别处做这件事」的兜底：
        日程链的地点限制由日程自己的开关决定，不能偷偷替它补一步移动。
        ``from_schedule=True``：这串动作是**用户排的日程**，不是她自己临时起意——
        「每天最多主动找他几次」那套额度只管她自己想找人，不该卡用户配好的日程。
        """

        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return False
        executed = False
        max_actions = self.world.limits.max_actions_per_message
        queued_actions = list(actions)[:max_actions]
        # 进入这一轮之前她自己没做完的安排：这一轮要排队时接在它们前面，而不是把它们顶掉。
        # 正在执行计划里的某一步时要跳过那一步，否则它会做第二遍。
        existing_plan = active_plan(state)
        # 她自己那一步还握在手上：那一步既不能再排一遍（会做两遍），
        # 也不能算进"还没做完的步骤"里
        running_index = self._running_plan_index(state, existing_plan)
        if running_index is not None:
            carried = self._plan_remaining_steps(existing_plan, after=running_index)
        else:
            carried = self._plan_remaining_steps(existing_plan, skip_current=from_plan)
        carried_reason = str((existing_plan or {}).get("reason") or "")
        carried_source = str((existing_plan or {}).get("source") or "")
        # 这一批是"有人跟她说话"（被动回复）还是她自己想做什么：
        # 排队久了以后，被动回复那几句不该被"主动发言冷却"当成刷屏吞掉
        batch_kind = outcome.speech_kind
        # 这一批里有没有动作把她的"手头那件事"换掉：只有那种情况才需要排队——
        # 她原来就在做的事不该拦住"说一句话"这种瞬时动作，否则对方等的是几分钟后的回话
        started_here = False
        tool_answered = False
        for index, action in enumerate(queued_actions):
            definition = self.world.action_map().get(action.type)
            if definition is None:
                outcome.notes.append(f"动作 {action.type} 不存在，已跳过")
                continue
            if not definition.enabled:
                # 停用 = 当作根本没有这个动作
                outcome.notes.append(f"动作 {action.type} 已停用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": action.type,
                        "note": f"「{definition.name or action.type}」已停用",
                    },
                    outcome=outcome,
                )
                continue
            if not self._action_usable_now(definition):
                outcome.notes.append(f"「{definition.name or definition.id}」当前时段不可用，已跳过")
                await self._log_event(state, "skip", {"action": definition.id, "note": outcome.notes[-1]}, outcome=outcome)
                continue
            if (
                tool_answered and action.type == "say"
                and self._speech_target(state, action, outcome) == outcome.home()
            ):
                outcome.notes.append("工具续写已回应本轮，跳过工具执行前预写的回答")
                continue
            if (
                definition.id in self.DEDUPE_ACTION_IDS
                and str(state.state) == STATE_DROWSY
                and autonomous
            ):
                # 临睡期里她自己又排了一次睡觉：那一觉已经揣在临睡期里了。
                # 再走一遍要么又排一个"睡觉"进计划（堆成四个），要么当场躺下
                # （好好的临睡期就被跳过了）——两种都不对，直接跳过并留痕。
                outcome.notes.append(
                    f"她已经在临睡期里了，「{definition.name or definition.id}」这一步跳过"
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": "已经在临睡期里（那一觉揣在临睡期里了），不再排一次",
                    },
                    outcome=outcome,
                )
                continue
            if definition.id in self.DEDUPE_ACTION_IDS and state.sleep_delayed:
                # 「等会儿再睡」：**不管这一路是谁推来的**（日程、大模型、被动回合）
                # 都不睡——她说好了再待一会儿。日程那条路（autonomous=False）也在这里拦下，
                # 不然 23:30 的 night_sleep 一到，答应的话就白说了。
                left = self.sleep_delay_minutes(state)
                outcome.notes.append(
                    f"刚说了「等会儿再睡」（还有 {left} 分钟），这一步不睡"
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"「等会儿再睡」的窗口里（还剩 {left} 分钟），这一步不睡",
                    },
                    outcome=outcome,
                )
                continue
            if (
                definition.id in self.DEDUPE_ACTION_IDS
                and autonomous
                and not skip_drowsy
                and int(state.world_time or 0) < int(state.no_sleep_until or 0)
            ):
                # 刚被叫醒 / 刚睡醒的保护期里她自己不再睡回去（规则决策那边本来就拦着，
                # 大模型这一路也得拦）——不然"叫醒她"等于没叫，精力满满也一直躺着。
                left = max(
                    0,
                    int(
                        (int(state.no_sleep_until) - int(state.world_time))
                        * self.tick_seconds
                        // 60
                    ),
                )
                outcome.notes.append(
                    f"刚醒没多久（还有 {left} 分钟保护期），这次不睡"
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"刚醒的保护期里（还剩 {left} 分钟），这一步不睡",
                    },
                    outcome=outcome,
                )
                continue
            if definition.id == "sleep" and self.world.sleep_node_id() and state.node_id != self.world.sleep_node_id():
                target = self.world.sleep_node_id()
                if find_path(self.world.adjacent(), state.node_id, target) is not None:
                    await self._travel_then_continue(state, outcome, definition, target,
                                                     queued_actions[index:], carried=carried)
                    return True
                outcome.notes.append(f"无法走到睡眠地点 {target}，这一步不睡")
                await self._log_event(state, "skip", {"action": definition.id, "note": outcome.notes[-1]}, outcome=outcome)
                continue
            if not definition.available_in(state.node_id) and not (definition.id == "sleep" and self.world.sleep_node_id() == state.node_id):
                # A2 兜底：她想去别处做这件事，自己又没写移动 —— 插件带她过去
                target = (
                    self._nearest_node_with_action(state.node_id, definition)
                    if bool(self.world.remote_action_travel) and allow_remote_travel
                    else ""
                )
                if target:
                    await self._travel_then_continue(
                        state,
                        outcome,
                        definition,
                        target,
                        queued_actions[index:],
                        carried=carried,
                    )
                    return True
                outcome.notes.append(f"动作 {action.type} 在 {state.node_id} 不可用，已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」在 {state.node_id} 不可用"},
                    outcome=outcome,
                )
                continue
            ok, reason = self._check_preconditions(state, definition)
            if not ok:
                outcome.notes.append(f"动作 {action.type} 前置条件不满足（{reason}），已跳过")
                await self._log_event(
                    state,
                    "skip",
                    {"action": action.type, "note": f"「{action.type}」前置条件不满足：{reason}"},
                    outcome=outcome,
                )
                continue
            if definition.llm_level == "tool":
                # 主模型只给了"想干什么"，参数交给辅助模型按工具定义补全
                if not await self._prepare_tool_action(
                    state, definition, action, outcome
                ):
                    continue
            if self._is_slow_action(definition):
                # 「等我两分钟～」这类排在慢动作前面说的话：先发出去，再去做这件慢事
                await self._flush_say(outcome)
            if (
                autonomous
                and not from_schedule
                and action.target
                and str(getattr(definition, "target_type", "")) == "user"
                and bool(getattr(self.world.profile, "enabled", True))
            ):
                # 她**主动**对某个人做事（抱抱、戳一戳这些）受"这一级每天最多主动找他几次"限制：
                # 陌生 / 客气级别是 0 次（不主动动手），越亲近额度越大。
                view = self.profiles.view(self.profile_session(state, action.target, outcome.place), action.target)
                left = self.profiles.proactive_quota_left(self.profile_session(state, action.target, outcome.place), action.target)
                if left <= 0:
                    cap = getattr(getattr(view, "level", None), "proactive_per_day", 0)
                    outcome.notes.append(
                        f"主动找 {action.target} 的次数今天用完了（这一级最多 {cap} 次），跳过这一步"
                    )
                    await self._log_event(
                        state,
                        "skip",
                        {
                            "action": action.type,
                            "note": f"主动找 {action.target} 的今日额度用完了（这一级 {cap} 次）",
                        },
                        outcome=outcome,
                    )
                    continue
                self.profiles.note_proactive(
                    self.profile_session(state, action.target, outcome.place), action.target, now=self._now()
                )
            before_action = state.current_action
            answered = await self._start_action(
                state,
                node,
                outcome,
                definition,
                action,
                depth,
                autonomous,
                from_plan=from_plan,
                skip_drowsy=skip_drowsy,
                # 工具后面已有当前会话的台词，不再额外触发默认续写；
                # 工具前的「等我查一下」不算结果回答。
                reply_planned=any(
                    item.type == "say" and bool(item.messages)
                    and self.world.action_map().get("say") is not None
                    and self.world.action_map()["say"].available_in(state.node_id)
                    and self._speech_target(state, item, outcome) == outcome.home()
                    for item in queued_actions[index + 1 :]
                ),
            )
            tool_answered = tool_answered or bool(answered)
            if state.current_action is not before_action:
                # 她手头换成了这一批里的动作：后面的才需要排队等它做完
                started_here = True
            if state.state == STATE_DROWSY and state.drowsy_sleep_step:
                # 这一步没真做，被揣进临睡期了：得从计划里划掉，否则醒来还会再做一遍。
                # 同一串里排在它后面的动作接在睡觉之后，等她睡下再继续。
                if from_plan:
                    advance(state, status="deferred")
                rest = queued_actions[index + 1 :]
                if rest:
                    self._queue_steps(
                        state,
                        outcome,
                        rest,
                        carried,
                        kind=batch_kind,
                        note=(
                            f"她正要睡下，剩下的 {len(rest)} 个动作排在她睡下之后"
                        ),
                    )
                return True
            if action.type != "walk_to" and state.pending_arrival:
                # 走到这儿之后紧接着就有安排（同一串动作 / 计划里的下一步）：
                # 「落地后再问一次大模型」是给"只移动、没说到了做什么"兜底的，
                # 这里已经有要做的事，就别再花一次调用
                state.pending_arrival = False
                outcome.notes.append(
                    f"落地后已经有「{definition.name or definition.id}」，不再额外决策"
                )
            executed = True
            # 她开始做一个要花时间的动作时，后面的动作不能立刻抢着执行，
            # 更不能把它顶掉——转成计划，等她忙完再做。
            #
            # 「她本来就忙着」是另一回事：手上的事不是这一批开的，就不该拦住一句
            # 瞬时的话（那是对方正等着的回话，排到十几分钟后就没意义了）。
            if state.current_action is not None:
                rest = queued_actions[index + 1 :]
                if rest and started_here:
                    self._queue_steps(
                        state,
                        outcome,
                        rest,
                        carried,
                        kind=batch_kind,
                        note="",
                    )
                    note = (
                        f"她开始「{definition.name or definition.id}」，"
                        f"剩下的 {len(rest)} 个动作排队等它做完"
                    )
                    if carried:
                        note += f"；原来没做完的 {len(carried)} 步接在它们后面"
                    outcome.notes.append(note)
                    return True
                if not rest:
                    return True
                # 剩下的是她正等着的回话：这一批里继续做掉，不排队
        return executed

    @staticmethod
    def _is_slow_action(definition: ActionDef) -> bool:
        """这个动作会不会花上一会儿：要调工具 / 指令 / 模型，或者要持续一段时间。"""

        if str(getattr(definition, "category", "instant")) != "instant":
            return True
        return str(getattr(definition, "llm_level", "")) in ("tool", "command")

    async def _flush_say(self, outcome: TickOutcome) -> None:
        """慢动作开始前，把她已经说出口的话**当场发出去**。

        没有这一步，「坐好等我两分钟～」会等整段检索（含读正文、补查）跑完，
        和结果一起冒出来——群里看起来就是她半天不吭声、然后一口气说三句。
        发出去的那几句记进 ``live_messages``：收尾只发剩下的，日志与聊天记录里算她说过话。
        ``say`` 排在慢动作后面（本来就是"做完再说"）时不受影响。
        """

        self._filter_proactive_speech(outcome)
        if self.say_sink is None or not outcome.messages:
            return
        kinds = outcome.speech_kinds.pop("", [])
        lines = outcome.take_messages()
        sent_count = 0
        for index, line in enumerate(lines):
            kind = kinds[index] if index < len(kinds) else outcome.speech_kind
            if not self.speech_allowed(outcome.home(), kind):
                sent_count += 1
                continue
            try:
                sent = bool(await self.say_sink(outcome.home(), line))
            except Exception as exc:
                self._log("debug", f"即时发言失败：{exc}")
                sent = False
            if not sent:
                # 发不出去（会话没配 / 平台在冷却里）：从这里开始都留到收尾一起发，别丢
                break
            outcome.live_messages.append(line)
            outcome.live_kinds.append(kind)
            sent_count += 1
        if sent_count >= len(lines):
            return
        outcome.messages = lines[sent_count:]
        outcome.speech_kinds[""] = kinds[sent_count:]
        # 回显的位置是"排在第几句之后"：前面几句已经当场发出去了，位置整体往前挪
        outcome.debug_positions = [
            max(0, int(position) - sent_count) for position in outcome.debug_positions
        ]

    async def _travel_then_continue(
        self,
        state: WorldState,
        outcome: TickOutcome,
        definition: ActionDef,
        target_node: str,
        pending: list[PlannedAction],
        *,
        carried: list[dict[str, Any]] | None = None,
    ) -> None:
        """她想去别处做某件事、自己又没写移动：先送她过去，剩下的排进计划。

        与规则决策器里的「先走再做」是同一套逻辑，只是这里替大模型补上那一步。
        """

        outcome.notes.append(
            f"她想做「{definition.name or definition.id}」，但当前地点做不了，"
            f"先带她去{self._node_name(target_node)}"
        )
        outcome.auto_travel.append(target_node)
        # 她把"说一句"排在移动后面时，这一轮会一个字都不发、还会被判定成接管失败。
        # 瞬时动作不占时间，先执行掉——用户立刻看到回应，剩下的等走过去再做。
        # 只有当前地点就做得成的才算：要换地方才做得成的那一步正是"为什么先走过去"。
        instant = []
        for item in pending:
            candidate = self.world.action_map().get(item.type)
            if candidate is None or candidate.category != "instant":
                continue
            if candidate.available_in(state.node_id):
                instant.append(item)
        for item in instant:
            await self._start_action(
                state,
                self.node(state.node_id),
                outcome,
                self.world.action_map()[item.type],
                item,
                0,
                False,
            )
        pending = [item for item in pending if item not in instant]
        if not pending:
            return
        tail = carried if carried is not None else self._plan_remaining_steps(active_plan(state))
        steps, dropped = self._dedupe_queued_steps(
            [self._step_payload(item, outcome.home(), outcome.speech_kind) for item in pending], tail
        )
        if dropped:
            outcome.notes.append(f"「睡觉」这一类动作已经排过了，重复的 {dropped} 步丢掉")
        if not steps:
            return
        await self._log_event(
            state,
            "plan",
            {
                "reason": f"为了做「{definition.name or definition.id}」先移动过去",
                "source": "auto_travel",
                "steps": [{"action": item.get("action")} for item in steps],
            },
            outcome=outcome,
        )
        walk = PlannedAction(type="walk_to", target_node=target_node)
        await self._start_action(
            state, self.node(state.node_id), outcome, self.world.action_map()["walk_to"],
            walk, 0, False,
        )
        if state.current_action is None:
            return
        # 她自己原来没做完的安排接在后面：主人这一轮的要求先做，再回去做她自己的事
        state.current_plan = create_plan(
            steps=steps + tail,
            world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
            valid_for=self.world.limits.plan_valid_duration,
            reason=f"到了{self._node_name(target_node)}之后要做的事",
            source="auto_travel",
        )

    def _node_name(self, node_id: str) -> str:
        node = self.node(node_id)
        return (node.name or node.id) if node else node_id

    def _check_preconditions(self, state: WorldState, definition: ActionDef) -> tuple[bool, str]:
        pre = definition.preconditions
        if pre.not_state and state.state in pre.not_state:
            return False, f"不能在 {state.state} 状态执行"
        if pre.min_energy is not None and state.energy < pre.min_energy:
            return False, "精力不足"
        return True, ""

    def _tool_exists(self, name: str) -> bool:
        return name in self.available_tools()

    # ---------------- 工具熔断 ----------------
    #
    # 连续两次"工具本身不可用"（没注册 handler / 抛异常 / 超时）就临时不用它，
    # 退避 30 秒起、逐次翻倍、上限 30 分钟。参数缺失或返回空结果**不算**失败。

    TOOL_BREAK_AFTER = 2
    TOOL_BREAK_BASE_SECONDS = 30.0
    TOOL_BREAK_MAX_SECONDS = 1800.0

    def _tool_breaker(self, name: str) -> dict[str, Any]:
        return self._tool_failures.setdefault(
            str(name or ""), {"count": 0, "until": 0.0, "reason": ""}
        )

    def tool_unavailable_reason(self, name: str) -> str:
        """这个工具现在是不是被熔断了；返回剩余秒数的说明，空串表示可用。"""

        record = self._tool_failures.get(str(name or ""))
        if not record:
            return ""
        until = float(record.get("until") or 0.0)
        if until <= 0:
            return ""
        left = until - self._now()
        if left <= 0:
            # 退避到期：放一次试探（half-open），成功就会清零
            record["until"] = 0.0
            return ""
        return f"刚失败过，{int(left)} 秒后再试（{_clip_text(record.get('reason'), 60)}）"

    def tool_breaker_state(self) -> list[dict[str, Any]]:
        """当前被熔断的工具（编辑器里显示 + 手动解除用）。"""

        now = self._now()
        items: list[dict[str, Any]] = []
        for name, record in self._tool_failures.items():
            until = float(record.get("until") or 0.0)
            if until <= now:
                continue
            items.append(
                {
                    "tool": name,
                    "seconds_left": int(until - now),
                    "fails": int(record.get("count") or 0),
                    "reason": _clip_text(record.get("reason"), 80),
                }
            )
        return sorted(items, key=lambda item: item["seconds_left"], reverse=True)

    def reset_tool_breakers(self, name: str = "") -> int:
        """解除熔断：给名字就清这一个，留空清全部（配置一改就全清）。"""

        if name:
            record = self._tool_failures.pop(str(name), None)
            return 1 if record else 0
        count = len(self._tool_failures)
        self._tool_failures.clear()
        return count

    def note_tool_result(self, name: str, *, ok: bool, error: str = "") -> None:
        """记一次工具调用的结果，决定要不要熔断。"""

        key = str(name or "")
        if not key:
            return
        if ok:
            # 成功即清零：这也是"暂不可用"最主要的自动解除方式
            self._tool_failures.pop(key, None)
            return
        if not _looks_like_tool_broken(error):
            return
        record = self._tool_breaker(key)
        record["count"] = int(record.get("count") or 0) + 1
        record["reason"] = str(error or "")
        if record["count"] >= self.TOOL_BREAK_AFTER:
            backoff = self.TOOL_BREAK_BASE_SECONDS * (2 ** (record["count"] - self.TOOL_BREAK_AFTER))
            record["until"] = self._now() + min(self.TOOL_BREAK_MAX_SECONDS, backoff)

    def _tool_hint(self, limit: int = 8) -> str:
        """给「缺少工具」的报错补上"现在到底有哪些工具"，方便用户自己改配置。"""

        names = sorted(self.available_tools())
        if not names:
            return "（AstrBot 里当前没有注册任何工具）"
        shown = "、".join(names[:limit])
        if len(names) > limit:
            shown += f" 等 {len(names)} 个"
        return f"（当前可用工具：{shown}）"

    def resolve_tool(self, action_id: str, node_id: str = "", tool_name: str = "") -> str:
        """这个动作最终会用哪个工具。

        工具型动作必须显式指定工具：名字已注册、且不是「直发消息」类工具才算可用。
        """

        name = str(tool_name or "").strip()
        if not name:
            return ""
        if not self._tool_exists(name):
            return ""
        if is_self_send_tool(name):
            # 别静默跳过：被挡下来的如果是出图这类工具，图只能等别的路径补发，
            # 落点就跟正文分家了——至少留一条日志，排查时有迹可循
            self._log("debug", f"工具「{name}」是直发消息类，插件不调用它")
            return ""
        if self.tool_unavailable_reason(name):
            # 刚连续失败过：退避期内不再用它（到点会自动放行试探）
            return ""
        return name

    def resolve_tool_prefix(self, tool_name: str) -> str:
        """按前缀找一个装了的工具：``web_search`` 能对上官方的 ``web_search_tavily``。

        只用在"只配了一个工具"的动作上：用户明确挑了哪几个工具的多工具链不做替换。
        """

        wanted = str(tool_name or "").strip().lower()
        if len(wanted) < 5:
            return ""
        for installed in sorted(self.available_tools()):
            if is_self_send_tool(installed):
                continue
            if str(installed).lower().startswith(wanted):
                return str(installed)
        return ""

    def resolve_tools(self, definition: ActionDef, node_id: str = "") -> list[str]:
        """这个动作最终会用到的工具：配了几个就返回几个，按配置顺序。

        ``tool_names`` 一个都没装时才轮到 ``tool_fallbacks``（内置搜索 / 查天气用得上），
        那时只取备选里第一个装了的——同一个意思的工具没必要挨个调一遍。
        """

        names = definition.tool_list()
        result: list[str] = []
        for name in names:
            chosen = self.resolve_tool(definition.id, node_id, name)
            if chosen and chosen not in result:
                result.append(chosen)
        if result:
            return result
        # 只配了一个工具的动作允许按前缀找（官方搜索工具叫 web_search_tavily 这种）
        allow_prefix = len(names) <= 1
        if allow_prefix:
            for name in names:
                chosen = self.resolve_tool_prefix(name)
                if chosen:
                    return [chosen]
        for name in definition.tool_fallbacks or []:
            chosen = self.resolve_tool(definition.id, node_id, name) or (
                self.resolve_tool_prefix(name) if allow_prefix else ""
            )
            if chosen and chosen not in result:
                result.append(chosen)
                break
        if not result and str(getattr(definition, "id", "")) == "check_weather":
            # 「查天气」没绑工具时自己认一把（uapi_weather 这类）：
            # 主回复的工具调用归原生 Agent，天气动作也不该逼用户去配工具型动作。
            candidates = self.weather_tool_candidates()
            if candidates:
                result.append(candidates[0])
        return result

    @model_operation("helper", "动作执行前补全工具参数")
    async def fill_tool_params(
        self,
        state: WorldState,
        definition: ActionDef,
        action: PlannedAction,
        *,
        tool_name: str = "",
        params: dict[str, Any] | None = None,
        previous_results: str = "",
        error_hint: str = "",
        extra_rules: str = "",
    ) -> tuple[dict[str, Any], str]:
        """把「她想干什么」翻译成工具需要的参数字典。

        返回 ``(params, note)``：note 非空表示没补全（原因），调用方据此决定跳过还是硬着头皮调。
        相同工具 + 相同意图会命中缓存，不会反复请求。

        ``previous_results`` 用于一个动作挂了多个工具的场景：把它传给补全模型，
        后面的工具就能用上前一个工具查回来的内容（例如先搜到网址、再去抓正文）。
        ``error_hint`` 是上一次调用报的错：带着它再补一次，能救回"schema 写可选、
        实现却必须要"这类工具。
        """

        base = dict(params if params is not None else (action.params or {}))
        chosen = self.resolve_tool(
            definition.id, state.node_id, tool_name or definition.tool_name
        )
        if not chosen or self.helper_llm is None:
            return {}, ""
        missing = self.missing_params_for(chosen, base)
        unfilled = self.unfilled_params(chosen, base)
        if not missing and not unfilled and not previous_results and not error_hint:
            # 已经齐了就不用问模型；但带了"上一个工具的结果"时是有意重算，必须再问一次
            return {}, ""
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句，
            # 否则这条工具动作到点就只会被跳过。
            intent = self.fallback_intent(definition)
        cache_key = (chosen, intent)
        # 带了上一个工具的结果、或者带了报错时都是"有意重算"，不吃缓存
        cached = (
            None
            if (previous_results or error_hint)
            else self._filled_params.get(cache_key)
        )
        if cached:
            return dict(cached), ""
        if not self._tool_param_allowed(state):
            return {}, "本小时的工具参数补全次数已用完"

        schema = self.tool_schemas().get(chosen) or {}
        description = self.available_tools().get(chosen, "")
        system_prompt, prompt = self.prompts.build_tool_params_prompt(
            tool_name=chosen,
            tool_description=description,
            param_text=render_param_text(schema),
            intent=intent,
            recent_chat=self.chat_window(state),
            previous_results=previous_results,
            error_hint=error_hint,
            extra_rules=extra_rules,
        )
        reply = await self._ask_judge(
            state.session_id, system_prompt, prompt
        )
        self._count_tool_param(state)
        if reply is None:
            return {}, "参数补全模型调用失败"
        payload = extract_json_object(reply)
        if not isinstance(payload, dict):
            return {}, "参数补全模型没有返回合法 JSON"
        filled = {
            str(key): value
            for key, value in payload.items()
            if not str(key).startswith("_")
        }
        if not filled:
            return {}, "参数补全模型没有给出参数"
        given = {k: v for k, v in base.items() if v not in ("", None)}
        # 平时以调用方给的参数为准；带上了上一个工具的结果时，以模型新给的为准（覆盖之前猜的）
        merged = {**given, **filled} if previous_results else {**filled, **given}
        still_missing = self.missing_params_for(chosen, merged)
        if still_missing:
            return {}, f"仍缺少参数 {still_missing}"
        if not previous_results:
            self._filled_params[cache_key] = dict(merged)
        if len(self._filled_params) > 200:
            self._filled_params.clear()
        return merged, ""

    async def _background_generate(self, channel: Any, **kwargs: Any) -> Any:
        return await self._budgeted_generate(channel, **kwargs)

    async def _ask_helper(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        if self.helper_llm is None:
            return None
        try:
            reply = await self._background_generate(self.helper_llm,
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.0,
            )
        except Exception as exc:
            self._log("debug", f"参数补全调用失败：{exc}")
            return None
        return reply.text if getattr(reply, "ok", False) else None

    async def _ask_judge(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """纯判断通道（挑一个 / 打分 / 抽字段）：走判断模型，没配就跟随打杂模型。

        判断模型不可用或报错时**自动退回打杂模型**——轻决策不该把主流程拖挂。
        """

        if self.judge_llm is not None and self.judge_llm is not self.helper_llm:
            try:
                reply = await self._background_generate(self.judge_llm,
                    session_id=session_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    temperature=0.0,
                )
                if getattr(reply, "ok", False) and str(getattr(reply, "text", "") or "").strip():
                    return reply.text
                self._log("debug", "判断模型没给出内容，这一轮退回打杂模型")
            except Exception as exc:
                self._log("debug", f"判断模型调用失败（{exc}），退回打杂模型")
        return await self._ask_helper(session_id, system_prompt, prompt)

    async def _ask_event(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """事件结算的模型通道（独立配置，留空跟随打杂模型）。

        它写的是"这件事最后怎么样了"——一句话定下场，还带着情绪与能力值的变化。
        这一段最吃"会不会写人话"，所以单独留了槽位。
        """

        if self.event_llm is not None and self.event_llm is not self.helper_llm:
            try:
                reply = await self._background_generate(self.event_llm,
                    session_id=session_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    temperature=0.4,
                )
                if getattr(reply, "ok", False) and str(getattr(reply, "text", "") or "").strip():
                    return reply.text
                self._log("debug", "事件模型没给出内容，这一轮退回打杂模型")
            except Exception as exc:
                self._log("debug", f"事件模型调用失败（{exc}），退回打杂模型")
        return await self._ask_helper(session_id, system_prompt, prompt)

    async def _ask_consolidate(
        self, session_id: str, system_prompt: str, prompt: str
    ) -> str | None:
        """睡眠整理的模型通道（独立配置，留空回落到辅助模型）。"""

        channel = self.consolidate_llm
        if channel is None:
            return None
        try:
            reply = await self._background_generate(channel,
                session_id=session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.4,
            )
        except Exception as exc:
            self._log("debug", f"睡眠整理调用失败：{exc}")
            return None
        return reply.text if getattr(reply, "ok", False) else None

    def _tool_param_allowed(self, state: WorldState) -> bool:
        hour = int(self._now() // 3600)
        if state.tool_param_hour_marker != hour:
            state.tool_param_hour_marker = hour
            state.tool_param_count_hour = 0
        return state.tool_param_count_hour < int(self.world.limits.max_tool_param_per_hour)

    @staticmethod
    def _count_tool_param(state: WorldState) -> None:
        state.tool_param_count_hour = int(state.tool_param_count_hour) + 1

    @staticmethod
    def _act_get(action: Any, key: str, default: Any = None) -> Any:
        """待执行动作有两种形态（PlannedAction / 运行中的 dict），统一读法。"""

        if isinstance(action, dict):
            return action.get(key, default)
        return getattr(action, key, default)

    @staticmethod
    def _act_set(action: Any, key: str, value: Any) -> None:
        if isinstance(action, dict):
            action[key] = value
        else:
            setattr(action, key, value)

    async def _prepare_tool_action(
        self,
        state: WorldState,
        definition: ActionDef,
        action: Any,
        outcome: TickOutcome,
    ) -> bool:
        """工具型动作动手前的准备：确认工具可用，并把每个工具的参数补齐。

        返回 ``False`` 表示这次跑不了（跳过原因已经记进日志）。补好的参数写在
        ``action.tool_params``（工具名 -> 参数字典），执行时按顺序逐个调用。
        """

        names = self.resolve_tools(definition, state.node_id)
        if not names:
            picked = "、".join(definition.tool_candidates())
            if not picked:
                reason = "工具型动作必须选一个工具，这个动作还没选"
            elif is_self_send_tool(definition.tool_list()[0] if definition.tool_list() else ""):
                reason = (
                    f"「{definition.tool_list()[0]}」是直发消息的工具，"
                    "插件不会调用（会绕过回复管线）"
                )
            else:
                reason = f"选的工具「{picked}」在 AstrBot 里没注册{self._tool_hint()}"
            outcome.notes.append(f"工具动作 {definition.id} 找不到可用工具，已跳过")
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"工具动作「{definition.name or definition.id}」找不到可用工具：{reason}",
                },
                outcome=outcome,
            )
            return False

        # 用户在动作里配好的固定参数（例如查天气固定 city=武汉）先铺底，
        # 大模型这一轮写出来的参数覆盖在它上面。
        base = self._action_base_params(definition, action)
        if str(getattr(definition, "tool_flow", "simple")) == "search":
            # 检索型动作不在这一步补参数：查询词和参数在真正调用时按查询逐条生成
            # （见 ``_run_search_flow``），这里只把这一轮要查什么定下来。
            self._act_set(action, "queries", self._explicit_queries(definition, action))
            self._act_set(action, "tool_params", {})
            self._act_set(action, "params", dict(base))
            return True
        stored: dict[str, dict[str, Any]] = {}
        for index, name in enumerate(names):
            # 大模型写出来的参数只当第一个工具的，其余工具由辅助模型按各自定义补
            params = dict(base) if index == 0 else {}
            missing = self.missing_params_for(name, params)
            unfilled = self.unfilled_params(name, params)
            note = ""
            if missing or unfilled:
                filled, note = await self.fill_tool_params(
                    state, definition, action, tool_name=name, params=params
                )
                if filled:
                    params = {**params, **filled}
                missing = self.missing_params_for(name, params)
            if missing:
                outcome.notes.append(
                    f"工具动作 {definition.id} 缺少必填参数 {missing}，已跳过"
                    + (f"（{note}）" if note else "")
                )
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": (
                            f"工具动作「{definition.id}」缺少必填参数 {missing}"
                            + (f"：{note}" if note else "")
                        ),
                    },
                    outcome=outcome,
                )
                return False
            stored[name] = params

        self._act_set(action, "tool_params", stored)
        self._act_set(action, "params", dict(stored.get(names[0]) or {}))
        return True
