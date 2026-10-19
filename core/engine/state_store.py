"""世界状态的读写、迁移与数值历史。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from ..config_scope import session_scoped
from ..memory import SCENE
from ..models import (
    ColdStartMode,
    NodeDef,
)
from ..mood import (
    SELF_CARE_NOTE,
    StyleCell,
    cell_for,
    style_block as render_style_block,
)
from ..state import (
    STATE_AWAKENING,
    STATE_IDLE,
    WorldState,
)
from .util import _as_float
from .life_inference import STATE_LEASE


class StateStoreMixin:
    # ================= 状态存取 =================

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def is_busy(self, session_id: str) -> bool:
        """她现在是不是正忙（有人在跑这个会话的动作/检索/模型调用）。

        只看会话锁，**不排队**：编辑器点「推进 tick」时用它做前置判断——
        忙着就直接跳过这一次，免得排队到动作结束后一口气推进好几个 tick。
        """

        # 锁挂在组代表会话上：按成员会话来问也要落到同一把锁
        lock = self._locks.get(self.state_key(session_id))
        return bool(lock is not None and lock.locked())

    @session_scoped
    async def load_state(self, session_id: str, *, cold_start: bool = True) -> WorldState:
        # 分过组的会话共用组代表会话的那一份：别的地方按成员会话来读时
        # 也要落到同一份上，不然会读到一份空壳。
        key = self.state_key(session_id)
        payload = await self.db.call("get_state", key)
        if payload is None:
            adopted = await self._adopt_group_state(key)
            if adopted is not None:
                return adopted
            return await self._cold_start(key) if cold_start else self._default_state(key)
        return WorldState.from_payload(payload, key)

    async def _adopt_group_state(self, key: str) -> WorldState | None:
        """刚把几个会话编成一组时，接过她原来那份状态。

        老数据是每个会话各存一份；合成一个她以后只认组代表那一份。
        代表名下还没有状态时，就挑组里"活得最久"的那份接着用，
        免得她一下回到刚出生、位置和数值全清零。
        """

        group = self.group_of(key)
        if group is None:
            return None
        best: WorldState | None = None
        for member in group.sessions or []:
            session_id = str(member)
            if session_id == str(key):
                continue
            try:
                payload = await self.db.call("get_state", session_id)
            except Exception:
                continue
            if payload is None:
                continue
            state = WorldState.from_payload(payload, key)
            if best is None or int(state.world_time) > int(best.world_time):
                best = state
        return best

    def _default_state(self, session_id: str) -> WorldState:
        defaults = self.world.default_state
        session = self.session_config(session_id)
        node_id = (
            session.cold_start_node
            if session and session.cold_start_node in self.world.node_map()
            else self.default_node_id()
        )
        state = WorldState(
            session_id=session_id,
            node_id=node_id,
            mood=defaults.mood,
            energy=defaults.energy,
            loneliness=defaults.loneliness,
            curiosity=defaults.curiosity,
            affect=defaults.affect,
            boredom=defaults.boredom,
            desire=defaults.desire,
            abilities=dict(self.world.abilities.initial),
            node_since=self._now(),
        )
        state.clamp()
        return state

    async def _cold_start(self, session_id: str) -> WorldState:
        """冷启动：生成初始状态与预设记忆。"""

        state = self._default_state(session_id)
        session = self.session_config(session_id)
        mode: ColdStartMode = session.cold_start_mode if session else "awakening"
        if mode == "awakening":
            state.state = STATE_AWAKENING
        elif mode == "silent":
            state.state = STATE_IDLE
        else:
            state.state = STATE_AWAKENING
        state.cold_start_done = False
        state.add_event("cold_start", {"mode": mode, "node": state.node_id})
        await self._seed_preset_memories(state)
        await self.save_state(state)
        return state

    async def _seed_preset_memories(self, state: WorldState) -> None:
        persona_id = ""
        for node in self.world.nodes:
            for preset in node.preset_memories:
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=persona_id,
                    node_id=node.id,
                    content=preset.content,
                    memory_type=SCENE,
                    emotion=preset.emotion,
                    weight=preset.weight,
                    scope=preset.scope,
                    source="preset",
                )

    async def save_state(self, state: WorldState) -> None:
        state.updated_at = time.time()
        state.clamp()
        await self.db.call("save_state", state.session_id, state.to_payload(), state.updated_at)

    @contextlib.asynccontextmanager
    @session_scoped
    async def session_state(self, session_id: str):
        """加锁 -> 加载 -> 交给调用方修改 -> 保存 -> 解锁。

        分过组的会话共用同一份状态（存在组代表会话名下）：几个群 / 私聊里的
        都是同一个她，位置、数值、计划、事件只有一份。
        """

        key = self.state_key(session_id)
        async with self.lock(key):
            state = await self.load_state(key)
            from ..planner import migrate_plan
            from ..life_matters import ensure
            migrate_plan(state, now=self._now(), tick_seconds=self.tick_seconds)
            ensure(state, self._now())
            self._sync_sleep_rhythm_mode(state)
            token = STATE_LEASE.set({"owner": asyncio.current_task(), "state": state,
                                     "lock": self.lock(key), "origin": session_id, "released": False,
                                     "persona": await self._persona_text(session_id)})
            try:
                yield state
            finally:
                try:
                    if (STATE_LEASE.get() or {}).get("save", True):
                        await self.save_state(state)
                finally:
                    STATE_LEASE.reset(token)


    # ---------------- 数值历史（给编辑器画曲线） ----------------

    async def _record_history(self, state: WorldState, node: NodeDef | None) -> None:
        """每个 tick 记一帧数值快照，顺手做数量裁剪。"""

        try:
            await self.db.call(
                "add_state_history",
                session_id=state.session_id,
                world_time=state.world_time,
                at=self._now(),
                values=self.dynamics.values(state),
            )
        except Exception as exc:  # 画曲线失败不能影响世界运行
            self._log("debug", f"写数值历史失败：{exc}")
            return
        count = self._history_writes.get(state.session_id, 0) + 1
        self._history_writes[state.session_id] = count
        if count % 60 == 0:
            try:
                await self.db.call(
                    "trim_state_history",
                    session_id=state.session_id,
                    keep=max(120, int(self.world.limits.max_history_rows)),
                )
            except Exception:
                pass

    async def state_history(self, session_id: str, *, hours: int = 24) -> dict[str, Any]:
        """取最近一段数值历史，并算出「她这段时间过得怎么样」的三个指标。"""

        span = max(1, int(hours)) * 3600
        since = self._now() - span
        rows = await self.db.call(
            "query_state_history",
            session_id=session_id,
            since=since,
            limit=max(120, int(self.world.limits.max_history_rows)),
        )
        series: list[dict[str, Any]] = []
        swing = 0.0
        peak_arousal = 0.0
        low_minutes = 0.0
        previous: dict[str, Any] | None = None
        for row in rows:
            item = {
                "at": float(row.get("at") or 0.0),
                "world_time": int(row.get("world_time") or 0),
                "affect": round(_as_float(row.get("affect")), 4),
                "valence": round(_as_float(row.get("valence"), 0.5), 4),
            }
            series.append(item)
            peak_arousal = max(peak_arousal, item["affect"])
            if previous is not None:
                minutes = max(0.0, (item["at"] - previous["at"]) / 60.0)
                swing += abs(item["affect"] - previous["affect"]) + abs(
                    item["valence"] - previous["valence"]
                )
                if item["valence"] < 0.35:
                    low_minutes += minutes
            previous = item
        return {
            "session_id": session_id,
            "hours": int(hours),
            "points": series,
            # 起伏：单位时间内的平均变化量；峰值：这段时间最激动到哪
            "metrics": {
                "swing_per_hour": round(swing / max(1.0, int(hours)), 3),
                "peak_arousal": round(peak_arousal, 3),
                "low_minutes": round(low_minutes, 1),
            },
        }

    def style_for(
        self, state: WorldState, session_id: str, *, direct: bool = False
    ) -> tuple[StyleCell | None, int, str]:
        """这一轮的表达方式：（格子、生效句数上限、进提示词的文字）。

        句数上限取**更严格者**：配置上限、格子上限、群聊硬顶（2 条），
        再加上"最近话太密"时的收紧——两条指令同时出现时不能互相抵消。

        ``direct``：这一轮是**有人正在跟她说话**（@ / 私聊 / 顺着话题对她说）。
        话太密时只提醒"短一点"，不再把她按到"能不说话就不说"——那样看着很呆。
        """

        configured = int(self.world.limits.max_messages_per_say or 3)
        if not bool(getattr(self.world, "style_injection", True)):
            state.last_style_cell = ""
            state.last_say_limit = max(1, configured)
            return None, state.last_say_limit, ""
        cell = cell_for(state.affect, state.valence)
        group = self.chat_is_group(session_id)
        limit = min(configured, cell.say_limit, 2 if group else 3)
        if self.speech_density_hint(state, direct=direct) and not direct:
            limit = min(limit, 1)
        limit = max(1, limit)
        state.last_style_cell = cell.key
        state.last_say_limit = limit
        # 长期低落时补一句"想自己待会儿"：安全阀是行为倾向，不是给她安排动作
        note = ""
        if float(state.valence) < 0.35:
            note = SELF_CARE_NOTE
        return cell, limit, render_style_block(
            cell, say_limit=limit, group=group, note=note
        )
