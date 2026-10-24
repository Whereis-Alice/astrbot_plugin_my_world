"""限额：动作次数与 LLM 预算。"""

from __future__ import annotations

from ..models import ActionDef
from ..state import WorldState


class LimitsMixin:
    # ================= 限额 =================

    def _hour_index(self, state: WorldState) -> int:
        return int(state.world_time * self.tick_seconds // 3600)

    def _count_autonomous(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            state.autonomous_hour_marker = hour
            state.autonomous_count_hour = 0
        state.autonomous_count_hour += 1

    def _within_hourly_limit(self, state: WorldState) -> bool:
        if self.world.limits.max_autonomous_per_hour <= 0:
            return False
        hour = self._hour_index(state)
        if state.autonomous_hour_marker != hour:
            return True
        return state.autonomous_count_hour < self.world.limits.max_autonomous_per_hour

    def reply_quota(self, state: WorldState) -> tuple[int, int]:
        """本小时被动回复的 ``(已用, 上限)``（上限 0 = 不限制）。

        分过组的会话共用同一份状态，所以这里天然就是"整组的额度"。
        """

        hour = self._hour_index(state)
        used = int(state.reply_count_hour or 0) if state.reply_hour_marker == hour else 0
        return used, max(0, int(self.world.limits.max_replies_per_hour or 0))

    def _passive_reply_allowed(self, state: WorldState) -> bool:
        """这一条被动回复还在额度内吗。"""

        used, limit = self.reply_quota(state)
        return limit <= 0 or used < limit

    def _count_passive_reply(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.reply_hour_marker != hour:
            state.reply_hour_marker = hour
            state.reply_count_hour = 0
        state.reply_count_hour = int(state.reply_count_hour or 0) + 1

    # ---------------- 动作的使用次数上限 ----------------

    def _usage_keys(self) -> dict[str, str]:
        """当前这三种周期各自的键（本地时间；换天/换周/换月自动换键 = 重新计数）。"""

        now = self.local_now()
        iso = now.isocalendar()
        return {
            "day": f"d:{now.strftime('%Y-%m-%d')}",
            "week": f"w:{iso[0]}-{iso[1]:02d}",
            "month": f"m:{now.strftime('%Y-%m')}",
        }

    def action_quota_left(self, state: WorldState, action: ActionDef, *, paid_keys: dict | None = None) -> int:
        """这个动作还剩几次可用（没有配额就返回一个很大的数）。"""

        quota = getattr(action, "quota", None)
        limits = quota.limits() if hasattr(quota, "limits") else {}
        table = dict((state.action_usage or {}).get(action.id) or {})
        keys = self._usage_keys()
        left: list[int] = []
        for period, limit in limits.items():
            if not limit or limit <= 0:
                continue
            key = keys.get(period, "")
            if paid_keys and paid_keys.get(period) == key:
                continue  # The same activity already paid for this period.
            used = int(table.get(key, 0) or 0)
            left.append(max(0, int(limit) - used))
        return min(left) if left else 10 ** 6

    def exhausted_actions(self, state: WorldState) -> set[str]:
        """这一轮不能再用的动作 id（配额用尽）。"""

        return {
            action.id
            for action in self.world.actions
            if self.action_quota_left(state, action) <= 0
        }

    def _count_action_use(self, state: WorldState, action: ActionDef, *, paid_keys: dict | None = None) -> None:
        """记一次动作使用（只记真正开始执行的那些）。"""

        table = dict((state.action_usage or {}).get(action.id) or {})
        keys = self._usage_keys()
        for period, key in keys.items():
            if paid_keys and paid_keys.get(period) == key:
                continue
            table[key] = int(table.get(key, 0) or 0) + 1
        # 只留最近的一小段，免得这个字典无限长
        if len(table) > 12:
            current = set(keys.values())
            table = {key: table[key] for key in sorted(table, key=lambda key: (key in current, key))[-12:]}
        state.action_usage = {**(state.action_usage or {}), action.id: table}

    def _arrival_decision_allowed(self, state: WorldState) -> bool:
        """「走到新地方」这类决策有独立的每小时上限（不占自主行动额度）。"""

        limit = max(0, int(self.world.limits.max_arrival_decisions_per_hour))
        if limit == 0:
            return False
        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            return True
        return state.arrival_count_hour < limit

    def _count_arrival_decision(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.arrival_hour_marker != hour:
            state.arrival_hour_marker = hour
            state.arrival_count_hour = 0
        state.arrival_count_hour += 1

    def _count_share(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            state.share_hour_marker = hour
            state.share_count_hour = 0
        state.share_count_hour += 1

    def _share_allowed(self, state: WorldState) -> bool:
        limit = max(0, int(self.world.limits.max_share_per_hour))
        if limit == 0:
            return False
        hour = self._hour_index(state)
        if state.share_hour_marker != hour:
            return True
        return state.share_count_hour < limit

    # ---------------- LLM 预算 ----------------

    def _llm_plan_allowed(self, state: WorldState) -> bool:
        """是否还允许问 LLM 要计划（间隔 + 每小时次数双重限制）。"""

        limits = self.world.limits
        if limits.max_llm_plan_per_hour <= 0:
            return False
        if self._now() - float(state.last_llm_plan_at or 0.0) < max(
            0, int(limits.llm_plan_min_interval_seconds)
        ):
            return False
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            return True
        return state.llm_plan_count_hour < max(0, int(limits.max_llm_plan_per_hour))

    def _count_llm_plan(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_plan_hour_marker != hour:
            state.llm_plan_hour_marker = hour
            state.llm_plan_count_hour = 0
        state.llm_plan_count_hour += 1
        state.last_llm_plan_at = self._now()

    def _llm_text_allowed(self, state: WorldState) -> bool:
        limit = max(0, int(self.world.limits.max_llm_text_per_hour))
        if limit == 0:
            return False
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            return True
        return state.llm_text_count_hour < limit

    def _count_llm_text(self, state: WorldState) -> None:
        hour = self._hour_index(state)
        if state.llm_text_hour_marker != hour:
            state.llm_text_hour_marker = hour
            state.llm_text_count_hour = 0
        state.llm_text_count_hour += 1

    def _fallback_text(self) -> str:
        """她没话可说时的兜底：**什么都不说**。

        以前这里从一句写死的通用短句池里随机挑一句（"……有人在吗？"）。
        那些句子跟任何人设都不搭，说出来只会让群里看到一句不属于这个角色的话；
        保持安静反而更像一个真的人。
        """

        return ""
