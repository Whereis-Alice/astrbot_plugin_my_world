"""对外接口：指令、编辑器与意图路由。"""

from __future__ import annotations

from typing import Any
from ..events import (
    ABILITIES,
    ABILITY_LABELS,
    MODE_IMAGINED,
    TIER_LABELS,
    ability_hint,
    normalize_abilities,
)
from ..memory import SCENE
from ..mood import keyword_signal
from ..state import WorldState
from .models import TickOutcome


class CommandsMixin:
    # ---------------- 对外接口（指令 / 编辑器 / 意图路由） ----------------

    def voice_sample_candidates_from_chat(
        self, state: WorldState, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        """从近期留档里挑"她自己说过、而且被接住了"的话，当样例候选。

        只挑被接住的：没人理的那几句进了样例库，等于把她教成"没人理也要继续说"。
        每条都带上当时对方说的那一句，用户才知道这句话该不该学。
        """

        rows = [item for item in list(state.recent_chat or []) if isinstance(item, dict)]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(rows):
            if not item.get("is_self"):
                continue
            name = str(item.get("name") or "")
            if name.startswith("（") or item.get("internal"):
                # 插件自己写的"她身上发生的事"，不是她说的话
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text or len(text) > 60:
                continue
            if any(mark in text for mark in ("[图片]", "[表情", "［图片", "［这条消息", "（")):
                continue
            origin = str(item.get("origin") or "")
            seq = int(item.get("seq") or 0)
            # 后面得有人说话，才算"被接住了"
            answered = any(
                str(later.get("origin") or "") == origin
                and not later.get("is_self")
                and not later.get("internal")
                and int(later.get("seq") or 0) > seq
                for later in rows[index + 1 :]
            )
            if not answered:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            # 上一句别人说的话：当作这条样例的上下文
            context = ""
            for earlier in reversed(rows[:index]):
                if str(earlier.get("origin") or "") != origin:
                    continue
                if earlier.get("is_self") or earlier.get("internal"):
                    continue
                context = " ".join(str(earlier.get("text") or "").split())[:40]
                break
            signal = keyword_signal(context)
            scene = {"hug": "tease", "negative": "snap", "positive": "comfort"}.get(
                signal, ""
            )
            out.append(
                {
                    "id": f"chat{len(out) + 1}",
                    "scene": scene,
                    "label": "",
                    "move": "",
                    "text": text[:120],
                    "context": context,
                    "source": "chat",
                }
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    async def pending_intervention(self, session_id: str) -> float:
        """她正在等协助吗？返回结束时间戳（0 = 不在等）。

        给「意图路由」用：这段时间里它会合并放行、免冷却，把群友的回应尽快交过来。
        """

        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return 0.0
        _, key = await self._journal_persona(session_id)
        info = self._pending_event_help(state, key)
        if str(info.get("session") or state.session_id) != session_id:
            return 0.0
        if str(info.get("state") or "") != "active":
            return 0.0
        try:
            until = float(info.get("active_until") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return until if until > self._now() else 0.0

    async def submit_event(self, session_id: str, text: str) -> str:
        """用户用指令投递一个事件（等于"强行掷骰命中"）。返回一句说明。"""

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        seed = " ".join(str(text or "").split())[:300]
        if not seed:
            return "要给她什么事？例如：/mw event 出门忘了带伞"
        if not self.is_enabled(session_id):
            return "这个会话还没启用她的世界"
        outcome: TickOutcome | None = None
        note = ""
        async with self.session_state(session_id) as state:
            self._ensure_abilities(state)
            now = self._event_time()
            # 忙就直说，**不排队**：排队会让人以为生效了，其实一直没发生
            blocked = self._event_block_reason(state, now, manual=True)
            active = self._active_thread(state)
            if blocked:
                note = f"这次没安排上：{blocked}。等她闲下来再投一次。"
            elif active is not None:
                title = str(active.get("title") or "一件事")
                note = f"她手上还有一件事没完（{title}）。等它收尾再投新的吧。"
            else:
                node = self.node(state.node_id) or self.node(self.default_node_id())
                outcome = TickOutcome(session_id=session_id)
                outcome.place = session_id
                state.event_recent_titles = list(state.event_recent_titles or [])
                await self._start_event(
                    state, node, outcome, tier="small", seed=seed, source="user"
                )
        if outcome is not None:
            await self._deliver(outcome)
        return note

    async def advance_thread(self, session_id: str, thread_id: str = "") -> str:
        """手动把没完的那件事往前推一幕（编辑器上的「立即推进」）。

        正常节奏是等 ``step_gap_seconds`` 到了才续演；这里就是"别等了，现在就来"。
        非要等群友拿主意的那件不推——那时候该等的是人，不是时钟。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if not self.is_enabled(session_id):
            return "这个会话还没启用她的世界"
        outcome: TickOutcome | None = None
        note = ""
        async with self.session_state(session_id) as state:
            now = self._event_time()
            thread = self._thread_by_id(state, thread_id) if thread_id else self._active_thread(state)
            if thread is None:
                note = "她现在没有没完的事"
            elif not self._event_belongs(state, thread):
                note = "这件事属于另一份人设，切回原人设后再推进"
            elif str(thread.get("status") or "open") != "open":
                note = "这件事已经完结了"
            elif self._thread_waiting_help(state, thread):
                note = "这件事她在等群友拿主意，先别替她往下演"
            elif not str(thread.get("pending_followup") or "").strip():
                note = "这件事没有下一步了"
            elif self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
                note = "她在睡觉，先叫醒再推进这件事"
            else:
                title = str(thread.get("title") or "一件事")
                node = self.node(state.node_id) or self.node(self.default_node_id())
                outcome = TickOutcome(session_id=session_id)
                outcome.place = session_id
                # 把"到点时间"挪到现在：续演那条路只认时间，不改它就推不动
                thread["next_step_at"] = float(now)
                advanced = await self._continue_thread(
                    state, node, outcome, now, thread_id=str(thread.get("id") or "")
                )
                if advanced:
                    note = f"推进了一幕：{title}"
                elif str(thread.get("status") or "") == "closed":
                    note = f"这件事挂太久了，已经收尾：{title}"
                else:
                    note = f"这次没推进：{title}（她可能正忙着，或者这件事已经收尾）"
        if outcome is not None:
            await self._deliver(outcome)
        return note

    async def close_thread(self, session_id: str, thread_id: str = "") -> str:
        """手动完结一件事（编辑器上的「立刻完结」）。

        她不再往下演：线索标成已完结、等着的那条也放掉，记忆里留一句收尾。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if not self.is_enabled(session_id):
            return "这个会话还没启用她的世界"
        async with self.session_state(session_id) as state:
            thread = self._thread_by_id(state, thread_id) if thread_id else self._active_thread(state)
            if thread is None:
                return "她现在没有正在进行的事"
            if not self._event_belongs(state, thread):
                return "这件事属于另一份人设，切回原人设后再完结"
            if str(thread.get("status") or "open") != "open":
                return "这件事已经完结了"
            now = self._event_time()
            title = str(thread.get("title") or "一件事")
            thread["status"] = "closed"
            thread["closed_at"] = float(now)
            thread["pending_followup"] = ""
            thread["next_step_at"] = 0.0
            # 正在等群友回话的那条一起放掉：不然下一次有人说话还会被她当成"在帮忙"
            info = dict(self._pending_event_help(state))
            if str(info.get("thread_id") or "") == str(thread.get("id") or ""):
                self._set_event_help(state, {})
            thread["pending_help"] = {}
            content = f"（{title}）这件事就到这里了"
            settled = str(thread.get("line") or "").strip()
            if settled:
                content = f"{settled}；这件事就到这里了"[:220]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=str(thread.get("place") or state.node_id),
                content=content,
                memory_type=SCENE,
                source="life_event",
                context=self._event_memory_context(thread, content),
                emotion=state.mood,
                weight=0.3,
                affect=state.affect,
                valence=state.valence,
            )
            await self._log_event(
                state,
                "event_idle",
                {"title": title, "stage": "stopped"},
            )
            return f"已完结：{title}"

    def _thread_waiting_help(self, state: WorldState, thread: dict[str, Any]) -> bool:
        """这条线索是不是正等着群友拿主意。"""

        info = self._pending_event_help(state)
        if str(info.get("state") or "") not in ("active", "idle"):
            return False
        return str(info.get("thread_id") or "") == str(thread.get("id") or "")

    async def event_overview(self, session_id: str) -> dict[str, Any]:
        """给编辑器与指令看的事件概览：能力值、当前这件事、以及最近的线索。

        每条线索都带上**每一幕发生了什么**（她选了什么、判定如何、结果怎样），
        编辑器的「历史事件」弹窗直接照着渲染，不用再拼日志。
        """

        state = await self.load_state(session_id, cold_start=False)
        values = normalize_abilities(state.abilities or self.world.abilities.initial)
        _, key = await self._journal_persona(session_id)
        threads = self._event_threads(state, key)
        opened = [item for item in threads if str(item.get("status") or "open") == "open"]
        info = self._pending_event_help(state, key)
        now = self._event_time()
        active = self._active_thread(state, key)
        active_id = str((active or {}).get("id") or "")
        help_thread_id = str(info.get("thread_id") or "")
        help_state = str(info.get("state") or "")
        return {
            "abilities": {
                name: {
                    "value": round(values[name], 3),
                    "label": ABILITY_LABELS[name],
                    "hint": ability_hint(values[name]),
                }
                for name in ABILITIES
            },
            "ability_day": state.ability_day,
            "ability_spent_today": dict(state.ability_spent_today or {}),
            "enabled": bool(getattr(self.world.events, "enabled", True)),
            "now": float(now),
            "world_time": int(state.world_time),
            "pending_help": {
                "state": help_state,
                "title": str(info.get("title") or ""),
                "until": float(info.get("active_until") or info.get("idle_until") or 0.0),
                "thread_id": help_thread_id,
            },
            "active_id": active_id,
            "threads": [
                self._thread_overview(
                    state,
                    item,
                    now=now,
                    active_id=active_id,
                    help_thread_id=help_thread_id,
                    help_state=help_state,
                )
                for item in threads[::-1]
            ],
            "open_count": len(opened),
            "recent_titles": list(state.event_recent_titles or []),
        }

    def _thread_overview(
        self,
        state: WorldState,
        thread: dict[str, Any],
        *,
        now: float,
        active_id: str,
        help_thread_id: str,
        help_state: str,
    ) -> dict[str, Any]:
        """一条线索给编辑器看的细节（含每一幕）。"""

        root = thread.get("root") or {}
        status = str(thread.get("status") or "open")
        tier = str(thread.get("tier") or "")
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(list(thread.get("steps") or []), start=1):
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "index": index,
                    "at": float(step.get("at") or 0.0),
                    "desc": str(step.get("desc") or ""),
                    "ability": str(step.get("ability") or ""),
                    "tier": str(step.get("tier") or ""),
                    "tier_label": str(
                        step.get("tier_label")
                        or TIER_LABELS.get(str(step.get("tier") or ""), "")
                    ),
                    "result": str(step.get("result") or ""),
                    "ability_delta": {
                        ABILITY_LABELS.get(str(name), str(name)): round(float(value), 3)
                        for name, value in dict(step.get("ability_delta") or {}).items()
                    },
                    "suggestions": int(step.get("suggestions") or 0),
                }
            )
        thread_id = str(thread.get("id") or "")
        waiting_help = status == "open" and thread_id == help_thread_id and help_state in ("active", "idle")
        can_advance = (
            status == "open"
            and bool(str(thread.get("pending_followup") or "").strip())
            and not waiting_help
        )
        return {
            "id": thread_id,
            "title": str(thread.get("title") or "一件事"),
            "hook": str(root.get("hook") or ""),
            "status": status,
            "tier": tier,
            "tier_label": str(TIER_LABELS.get(tier, "")),
            "kind": str(root.get("kind") or ""),
            "critical": bool(root.get("critical")),
            "imagined": str(root.get("mode") or "") == MODE_IMAGINED,
            "place": str(thread.get("place") or ""),
            "place_name": str(thread.get("place_name") or ""),
            "genre": str(thread.get("genre") or ""),
            "tone": str(thread.get("tone") or ""),
            "opened_at": float(thread.get("opened_at") or 0.0),
            "updated_at": float(thread.get("updated_at") or 0.0),
            "closed_at": float(thread.get("closed_at") or 0.0),
            "line": str(thread.get("line") or ""),
            "pending_followup": str(thread.get("pending_followup") or ""),
            "next_step_at": float(thread.get("next_step_at") or 0.0),
            "steps": steps,
            "step_count": len(steps),
            "is_active": thread_id == active_id,
            "waiting_help": waiting_help,
            "suspended": status == "open"
            and self._thread_suspended(state, thread, now)
            and not waiting_help,
            "can_advance": can_advance,
            "can_close": status == "open",
        }
