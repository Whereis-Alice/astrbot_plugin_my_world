"""配置装载与扩展挂载点。"""

from __future__ import annotations

from typing import Any
from ..config_scope import global_config
from ..consolidate import Consolidator
from ..decider import Decider
from ..engagement import EngagementTracker
from ..memory import MemoryEngine
from ..models import ActionDef
from ..profile import ProfileStore
from ..prompt import PromptBuilder
from ..state import WorldState
from ..state_dynamics import StateDynamics


class ConfigMixin:
    # ================= 配置 =================

    # ---------------- 扩展挂载点 ----------------

    def _apply_extension_actions(self, world: Any) -> None:
        """把扩展包注册的动作合进动作库。

        撞名的以原有动作为准（用户自己配过的东西不该被扩展悄悄改掉）；
        扩展给来的定义跑一遍校验，坏的就丢掉并记一条日志。
        """

        raw = self.extensions.actions()
        if not raw or world is None:
            return
        existing = {str(item.id) for item in getattr(world, "actions", []) or []}
        for item in raw:
            try:
                definition = ActionDef.model_validate(dict(item))
            except Exception as exc:
                self._log("warning", f"扩展动作定义不对，已忽略：{exc}")
                continue
            if not definition.id or definition.id in existing:
                continue
            definition.builtin = False
            world.actions.append(definition)
            existing.add(definition.id)

    def _extension_prompt(self, state: WorldState, session_id: str = "") -> str:
        """扩展要加进提示词的那一段（没装扩展就是空串）。"""

        try:
            return self.extensions.prompt_text(state, session_id or state.session_id)
        except Exception:
            return ""

    def _extension_gate(self, definition: ActionDef, state: WorldState) -> str:
        """扩展不让做这件事时给一句原因（空串 = 允许）。"""

        try:
            return self.extensions.gate_reason(definition, state, state.session_id)
        except Exception:
            return ""

    def _hidden_actions(self, state: WorldState, session_id: str = "") -> set[str]:
        """这一轮连名字都不该出现的动作：配额用尽的 + 扩展要求藏起来的。

        只在执行时拦不够——动作名出现在"你能写的 type"清单里就是泄漏。
        """

        picked = set(self.exhausted_actions(state))
        sid = str(session_id or state.session_id or "")
        # 扩展说"这个会话里不让做"的动作，连名字都不该出现——
        # 直接用它的 gate 判一遍，不指望扩展再单独维护一份隐藏名单
        for definition in getattr(self.world, "actions", []) or []:
            try:
                if self.extensions.gate_reason(definition, state, sid):
                    picked.add(str(definition.id))
            except Exception:
                continue
        try:
            picked.update(self.extensions.hidden_actions(state, sid))
        except Exception:
            pass
        return picked

    @global_config
    def reload_config(self) -> list[str]:
        """热加载配置：世界 / 日程 / 会话。"""

        world, schedules, sessions, warnings = self.store.reload()
        # 日程内容变了（改了时间/星期/动作）就把检查游标往回拨一次，
        # 否则「刚把时间改到现在」的日程会被当成已经检查过的过去。
        signature = [
            (
                item.id,
                bool(item.enabled),
                str(item.time),
                tuple(item.days or []),
                tuple(item.sessions or []),
                int(item.priority),
            )
            for item in (schedules.schedules if schedules is not None else [])
        ]
        signature.append(tuple((item.session_id, repr(item.config_overrides)) for item in sessions.sessions))
        if self._schedule_signature is not None and signature != self._schedule_signature:
            self._schedule_reset_pending = True
        self._schedule_signature = signature
        # 扩展包带来的动作：合并进动作库（坏的定义直接丢，不影响原动作）
        self._apply_extension_actions(world)
        self.world = world
        self.schedules = schedules
        self.sessions = sessions
        self._sessions_index = {item.session_id: item for item in sessions.sessions}
        # 会话组：几个群 / 私聊算同一个她。这里建两张反查表，
        # 提示词、记忆、聊天上下文都靠它们找到"同组的其它会话"。
        self.session_groups = list(getattr(sessions, "groups", None) or [])
        self._group_index: dict[str, Any] = {
            item.id: item for item in self.session_groups
        }
        self._group_of: dict[str, str] = {}
        for group in self.session_groups:
            for member in group.sessions or []:
                self._group_of[str(member)] = group.id
        self.load_warnings = warnings
        # 配置一改就清掉工具熔断：最常见的"工具坏了"其实是刚装好 / 刚补上 key，
        # 用户保存之后应该立刻能再试，而不是等退避结束。
        self._tool_failures.clear()

        self._base_runtime = self._make_runtime(world, schedules)
        self.db.raw.split_profile_sessions({item.session_id: self.state_key(item.session_id)
                                            for item in sessions.sessions})
        self._scope_runtimes.clear()
        return warnings

    def _make_runtime(self, world, schedules) -> dict[str, Any]:
        # 情绪两轴需要"现在是几点"和"现实时间"：交给演化器自己取，
        # 免得每个调用点都要把 now / hour 传一遍。
        dynamics = StateDynamics(
            world.state_dynamics,
            now_provider=self._now,
            hour_provider=lambda: self.local_now().hour,
            datetime_provider=self.local_at,
        )
        # 记忆按会话各存一份，召回时把同组的会话一起当条件（改组不会把记忆挂错地方）
        memory = MemoryEngine(self.db.raw, world, sessions_of=self.group_sessions)
        memory.semantic.provider = self.embedding
        # 用户画像：按实际会话各存一份，记忆回答"发生过什么"，它回答"这个人是谁"
        profiles = ProfileStore(
            self.db.raw, world, group_of=self.state_key, day_provider=self._today_key
        )
        # 睡眠整理：消化记忆 + 更新画像 + 写梦（小睡只做轻整理）
        consolidator = Consolidator(
            world=world,
            memory=memory,
            profiles=profiles,
            sessions_of=self.group_sessions,
            # 整理材料要标来源会话（群 / 私聊）：用和人话提示词同一套标签口径
            session_label=self.session_label,
            datetime_provider=self.local_at,
        )
        decider = Decider(
            world,
            now_provider=self._now,
            # 规则要知道"现在几点"：夜里该睡整觉，白天只小睡
            hour_provider=lambda: self.local_now().hour,
            tick_seconds=self.tick_seconds,
        )
        engagement = EngagementTracker(world)
        prompts = PromptBuilder(
            world, tick_seconds=self.tick_seconds, now_provider=self.local_now
        )
        consolidator.prompts = prompts
        consolidator.set_world(world)
        return dict(world=world, schedules=schedules, dynamics=dynamics, memory=memory,
                    profiles=profiles, consolidator=consolidator, decider=decider,
                    engagement=engagement, prompts=prompts)
