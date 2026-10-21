"""想被碰一碰：渴望、想念与主动靠近。"""

from __future__ import annotations

from typing import Any
from ..models import (
    NodeDef,
    action_intimacy,
)
from ..mood import day_mood_branch
from ..state import WorldState
from ..speech_policy import speech_origin
from .constants import (
    ASK_ABOUT_CURIOSITY,
    OUTDOOR_HINT_HOURS,
)
from .models import (
    MessageContext,
    TickOutcome,
)


class CuddleMixin:
    # ---------------- 想被碰一碰：交给她自己安排 ----------------
    #
    # 这里**不替她决定动作**：只把"你现在很想被人碰一下"+ 手边有哪些人、
    # 哪些肢体接触、可以在哪些会话里做，一起交给大模型，让她自己挑一个。
    # （规则决策器只知道数值、不认识人，硬塞一个动作会变成"她一想要就去抱"，
    #   抱谁、抱成什么样、在群里还是私聊，那些都得看关系和她当时的心情。）

    def desire_push_ready(self, state: WorldState) -> bool:
        """现在该不该把这一轮交给大模型，让她自己想想怎么贴。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "desire_push_enabled", True)):
            return False
        if float(getattr(state, "desire", 0.0) or 0.0) <= 0.0:
            return False
        if not self._desire_push_left(state):
            return False
        gap = max(0, int(getattr(config, "desire_push_min_interval_minutes", 40) or 0))
        last = float(getattr(state, "desire_push_at", 0.0) or 0.0)
        if gap and last and self._now() - last < gap * 60:
            return False
        # 今天的基调也会拨一下：黏人的日子更容易"想要"，想独处的日子基本不会
        try:
            strength = float(getattr(config, "daily_mood_strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        weight = day_mood_branch(
            str(getattr(state, "day_mood", "") or ""), "cuddle", strength=strength
        )
        threshold = float(getattr(config, "desire_push_threshold", 0.75) or 0.75)
        return float(state.desire) * weight >= threshold

    def _desire_push_left(self, state: WorldState) -> int:
        """生活组剩余实际联系额度；动机评估次数只作运行记录。"""

        today = self._today_key(self._now())
        if str(state.desire_push_day or "") != today:
            state.desire_push_day = today
            state.desire_push_count = 0
        return self.contact_status(state.session_id)["left"]

    def _cuddle_people(self, state: WorldState, session_id: str) -> list[dict[str, Any]]:
        """能去贴的人，**按亲密度从高到低**排。

        每人带上：现在是什么关系档、这一档明确不许的动作、今天还剩几次主动找他的额度。
        """

        rows: list[dict[str, Any]] = []
        presence = state.user_presence or {}
        for user_id in list(presence.keys()):
            uid = str(user_id or "").strip()
            if not uid:
                continue
            view = self.profiles.view(self.profile_session(state, uid, session_id), uid)
            if view is None or self._miss_ignored(view):
                continue
            status = self.contact_status(self.profile_session(state, uid, session_id), uid)
            if status["reason"]:
                continue
            level = getattr(view, "level", None)
            rows.append(
                {
                    "user_id": uid,
                    "name": str(getattr(view, "name", "") or uid),
                    "affinity": float(getattr(view, "affinity", 0.0) or 0.0),
                    "level": str(getattr(level, "name", "") or ""),
                    "deny": [str(item) for item in (getattr(level, "deny", None) or [])],
                    "quota_left": int(
                        self.profiles.proactive_quota_left(self.profile_session(state, uid, session_id), uid) or 0
                    ),
                    "places": self._person_places(state, uid),
                }
            )
        rows.sort(key=lambda item: (-item["affinity"], item["user_id"]))
        return rows

    @speech_origin("outreach")
    async def _maybe_desire_push(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> bool:
        """「很想被人碰一碰」：把这一轮交给大模型，让她自己安排。

        提示词里给全三件事——**现在什么状态**、**能找谁**（按亲密度排，附关系档与
        那条档位不许的动作）、**能做哪些肢体接触**（附亲密程度），然后让她自己决定
        去哪说（``send_to``）、做什么、对谁做。什么都不想做也可以，写别的计划就行。
        """

        if not self.proactive_chat_allowed(session_id):
            return False
        if self.llm is None or not self.desire_push_ready(state):
            return False
        people = [
            item for item in self._cuddle_people(state, session_id) if item["quota_left"] > 0
        ]
        selfs = self._self_comfort_options(state)
        if not people and not selfs:
            # 既没人可找、也没有"自己解决"这种动作 → 这一轮没什么可摆给她的
            return False
        actions = (
            self._cuddle_action_lines(state, people[0].get("deny") or []) if people else []
        )
        who_lines = []
        for item in people[:4]:
            deny = "、".join(
                self.world.action_map().get(name, None).name or name
                if self.world.action_map().get(name, None)
                else name
                for name in (item["deny"] or [])[:6]
            )
            where = f"；他最近在：{item['places']}" if item["places"] else ""
            who_lines.append(
                f"- {item['name']}（好感 {item['affinity']:.0f}，{item['level'] or '还不熟'}；"
                f"今天还能主动找他 {item['quota_left']} 次{where}）"
                + (f"。**跟他不能做**：{deny}" if deny else "")
            )
        parts = [
            f"你此刻希望获得陪伴或接触的感觉较强（欲求 {float(state.desire):.2f}）。"
            "这是当前状态，不规定你必须亲昵、撒娇或联系别人。"
            "如何理解和回应这份感觉，请遵循原有人设和当前情境。"
        ]
        if who_lines:
            parts.append(
                "你能找的人（越靠前越亲近）：\n"
                + "\n".join(who_lines)
                + "\n\n能做的肢体接触（括号里是贴得多近，0~1）：\n"
                + ("\n".join(actions) or "（这个地点这会儿没有能做的接触动作）")
                + "\n\n若决定联系，请按话题隐私、关系和场合选择群聊或私聊（计划里写 send_to）。"
            )
        else:
            parts.append(
                "这会儿没有能找的人（不在身边、或者今天的次数已经用完了）——"
                "那就别硬凑上去。"
            )
        if selfs:
            listed = "、".join(f"{action_id}（{label}）" for action_id, label in selfs)
            parts.append(
                f"也可以选择不涉及他人的活动：{listed}。是否适合、会有什么感受，以人设与实际情境为准。"
            )
        parts.append(
            "**也可以什么都不做**：如果这会儿不方便、或者你其实不太想，"
            "那就写个别的计划（看会儿书、发会儿呆都行），不用勉强。"
        )
        hint = "\n\n".join(parts) + "\n" + self.autonomous_prompt_note(state)
        plan = await self._ask_llm_for_plan(
            state, node, outcome, force=True, hint=hint, with_extension=True
        )
        state.desire_push_at = self._now()
        state.desire_push_count = int(state.desire_push_count or 0) + 1
        if plan is None:
            outcome.notes.append("想被人碰一碰，但这一轮没想出要做什么")
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=session_id
        )
        await self._apply_plan(state, node, outcome, plan)
        self._count_autonomous(state)
        await self._log_event(
            state,
            "desire_push",
            {
                "desire": round(float(state.desire), 3),
                "people": [item["name"] for item in people[:4]],
                "self_options": [action_id for action_id, _label in selfs],
                "plan": plan.get("steps"),
                "reason": plan.get("reason"),
            },
            outcome=outcome,
        )
        return True

    def _self_comfort_options(self, state: WorldState) -> list[tuple[str, str]]:
        """扩展提供的"不用找人也能解决"的动作：动作上打了 ``self_option`` 标记的。

        主插件不认识具体是哪个扩展：**谁注册了带这个标记的动作，就多出那个选项**。
        没装扩展 → 空列表 → 那一轮只摆「找谁」和「什么都不做」。
        """

        picked: list[tuple[str, str]] = []
        for definition in self.world.actions_in(state.node_id):
            if not bool(getattr(definition, "enabled", True)):
                continue
            label = str(getattr(definition, "self_option", "") or "").strip()
            if not label:
                continue
            picked.append((definition.id, label))
        return picked

    def _cuddle_action_lines(
        self, state: WorldState, deny: list[str] | None = None
    ) -> list[str]:
        """这个地点现在能做的"肢体接触"动作，一行一个（附亲密程度）。"""

        blocked = {str(item) for item in (deny or [])}
        rows: list[tuple[float, str]] = []
        for definition in self.world.actions_in(state.node_id):
            if not bool(getattr(definition, "enabled", True)):
                continue
            if str(getattr(definition, "target_type", "none") or "none") != "user":
                continue
            if definition.id in blocked:
                continue
            score = action_intimacy(definition)
            if score <= 0:
                continue
            rows.append((score, definition.id))
        rows.sort(key=lambda item: -item[0])
        lines: list[str] = []
        for score, action_id in rows[:10]:
            definition = self.world.action_map().get(action_id)
            name = (definition.name or action_id) if definition is not None else action_id
            lines.append(f"- {action_id}（{name}，{score:.1f}）")
        return lines

    def desire_panel(self, state: WorldState) -> dict[str, Any]:
        """编辑器「想念」那一栏旁边要显示的欲求信息（含"还要多久才轮到她主动"）。"""

        config = self.world.state_dynamics
        return {
            "value": round(float(getattr(state, "desire", 0.0) or 0.0), 3),
            "threshold": round(
                float(getattr(config, "desire_push_threshold", 0.75) or 0.75), 3
            ),
            "enabled": bool(getattr(config, "desire_push_enabled", True)),
            "push_left": self._desire_push_left(state),
            "push_cap": self.contact_status(state.session_id)["cap"],
            "last_push_at": float(getattr(state, "desire_push_at", 0.0) or 0.0),
        }

    @speech_origin("outreach")
    async def _maybe_miss_push(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        session_id: str,
    ) -> bool:
        """「想他想到不行了」：软推一次，让她自己去把他找出来。

        在哪说由她自己定：想私密、想撒娇就发私聊；想让别人看到关系好就发群里——
        提示词里把她能说话的地方都摆出来（``session_directory``），她照着挑一个写 ``send_to``。
        """

        if not self.proactive_chat_allowed(session_id):
            return False
        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return False
        if not bool(getattr(config, "miss_push_enabled", True)):
            return False
        if self._miss_push_left(state) <= 0:
            return False
        # 有多想主动找他：今天的基调、欲求，以及"孤独感偏高"（原来那条
        # "走去大厅喊一声"的规则删掉之后，孤独就靠这里接住）
        eagerness = self.decider.reach_out_eagerness(state)
        if float(getattr(state, "loneliness", 0.0) or 0.0) > 0.7:
            eagerness *= 1.4
        candidate = self._top_miss_candidate(state, eagerness=eagerness)
        if candidate is None or self.llm is None:
            return False
        uid, score = candidate
        profile = self.profiles.profile(self.profile_session(state, uid), uid)
        payload = dict((profile or {}).get("payload") or {})
        names = [str(item) for item in (payload.get("names") or []) if str(item)]
        name = names[-1] if names else str(payload.get("qq_name") or uid)
        idle = self._miss_idle_text(state, uid, profile or {})
        where = self._person_places(state, uid)
        hint = (
            f"你这会儿特别想 {name}（{idle or '刚想起来'}），想去跟他说句话。\n"
            "是否联系、说些什么，都遵循原有人设和当前情境；也可以先继续自己的生活。"
            "若决定联系，按话题隐私与场合选择群聊或私聊。\n"
            "注意：**换地方说 ≠ 把刚才那句话再说一遍**。"
            "如果你刚在群里说过一句、没人理，那就别把它原样搬到私聊——"
            "没有回应就先放下；有新的具体联系缘由再联系，不借自己的生活进展另起汇报。\n"
            f"他现在能在这些地方跟你说上话：{where or '（还没记录到他最近在哪儿说话）'}。\n"
            + self.autonomous_prompt_note(state)
            + "\n按你自己的节奏安排这一轮要做什么，输出计划 JSON。"
        )
        plan = await self._ask_llm_for_plan(state, node, outcome, force=True, hint=hint, focus_user=uid)
        if plan is None:
            outcome.notes.append(f"想找 {name}，但这一轮没想出要说什么")
            # 想不出来就先把这个人的想念压一半，别每轮都来问一次
            state.miss[uid] = max(0.0, score * 0.5)
            return False
        outcome.session_id = self._freeze_plan_target(
            state, outcome, plan, fallback=session_id
        )
        plan["contact_people"] = [uid]
        await self._apply_plan(state, node, outcome, plan)
        self._count_autonomous(state)
        state.miss[uid] = 0.0
        # 刚找过他：下一次"想他"重新随机等一段时间
        state.miss_ready_at[uid] = self._next_miss_open_at(self._now())
        state.miss_push_count = int(state.miss_push_count or 0) + 1
        await self._log_event(
            state,
            "miss_push",
            {
                "user": uid,
                "score": round(score, 3),
                "note": str(plan.get("reason") or "")[:80],
            },
            outcome=outcome,
        )
        self._log("debug", f"[my_world] 想他想到主动去找：{uid}")
        await self._tick_plan(state, node, outcome, depth=0)
        return True

    def miss_block(self, state: WorldState) -> str:
        """「有点想他」那一段：超过阈值的人才写，附带他能在哪儿说话。"""

        config = self.world.profile
        if not bool(getattr(config, "enabled", True)):
            return ""
        threshold = float(getattr(config, "miss_threshold", 0.6) or 0.6)
        limit = max(1, int(getattr(config, "miss_limit", 3) or 3))
        rows: list[tuple[float, str]] = []
        for user_id, value in (state.miss or {}).items():
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if score < threshold:
                continue
            rows.append((score, str(user_id)))
        rows.sort(reverse=True)
        lines: list[str] = []
        for _score, user_id in rows[:limit]:
            view = self.profiles.view(self.profile_session(state, user_id), user_id)
            if view is None or self._miss_ignored(view):
                continue
            profile = self.profiles.profile(self.profile_session(state, user_id), user_id)
            payload = dict((profile or {}).get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            name = (
                names[-1]
                if names
                else str(getattr(view, "name", "") or payload.get("qq_name") or user_id)
            )
            idle = self._miss_idle_text(state, user_id, profile)
            where = self._person_places(state, user_id)
            body = f"- {name}"
            if idle:
                body += f"（{idle}）"
            if where:
                body += f"：找他可以说给 {where}"
            else:
                body += "：你有点想他"
            lines.append(body)
        if not lines:
            return ""
        return (
            "# 你有点想他们了\n"
            "（不用马上做什么；真想找人就主动说一句，落点写上面那个）\n" + "\n".join(lines)
        )

    def extra_reminders(
        self, state: WorldState, *, autonomy: bool = False, ctx: Any = None, include_life: bool = True
    ) -> str:
        """给提示词的几段提醒：有点想谁 + 刚想起的事（后者读过一次就不再重复提）。

        ``autonomy``：这一轮是"她自己决定要做什么"（不是回谁的话）。只有这时候才把
        「好奇心上来了，想去问问谁」带进去——回话时带这段等于把话题拐到第三个人身上。

        ``ctx``：正在跟谁说话。心事 / 记着的账 / 她自己的事都是**分人**的，
        不回话的场合（``ctx`` 为空）就按"她自己心里的事"带上名字。
        """

        # Adaptive directed turns stay with the present people. Social planning
        # may consider whom to contact; an ordinary reply needn't hear that motive.
        parts = [self.miss_block(state)] if autonomy or include_life else []
        if autonomy or include_life:
            parts.append(self._greet_hint(state))
            parts.append(self._outdoor_hint(state))
        parts.append(self._heart_knot_block(state, ctx, autonomy=autonomy))
        parts.append(self._grudge_block(state, ctx, autonomy=autonomy))
        if include_life:
            parts.append(self._own_topic_block(state, ctx, autonomy=autonomy))
        if autonomy:
            parts.append(self._ask_about_block(state))
        pending = str(getattr(state, "pending_review", "") or "").strip()
        if pending:
            parts.append(pending)
            state.pending_review = ""
        return "\n\n".join(part for part in parts if part)

    def _outdoor_hint(self, state: WorldState) -> str:
        """一直窝在家里时的一句提醒：想出去走走就直接 walk_to。

        没有这一句她能在客厅连着待好几天——规则那条"出门"容易被别的动机挤掉，
        大模型看着满满一屋子能做的事，也不会主动往外走。
        """

        if not self.world.is_home_zone(state.node_id):
            return ""
        now = self._now()
        last = float(getattr(state, "last_outdoor_at", 0.0) or 0.0)
        hours = (now - last) / 3600.0 if last else 0.0
        if last and hours < OUTDOOR_HINT_HOURS:
            return ""
        said = float(getattr(state, "outdoor_hint_at", 0.0) or 0.0)
        if said and (now - said) < OUTDOOR_HINT_HOURS:
            # 说过一次就别每轮念：隔几个小时再说
            return ""
        span = f"已经 {int(hours)} 小时没出过门了" if last else "这两天一直在家里"
        state.outdoor_hint_at = now
        return (
            f"你{span}——想出去走走就直接 `walk_to` 一个外面的地点"
            "（街口 street_gate / 商场 mall / 小吃街 snacks / 公园公园大门 park_gate / "
            "湖边 park_lake / 海滨步道 boardwalk…），一次写清目标就行；"
            "在外面逛一逛、买点东西、看看新鲜的，比一直在家转圈有意思。"
        )

    def _note_greet_candidate(
        self, state: WorldState, ctx: MessageContext, *, now: float
    ) -> None:
        """有人隔了大半天又冒头 → 记一笔，让提示词里提一句"这人好久没来了"。

        只在群里算：私聊本来就是一对一，谈不上"回来打个招呼"。
        阈值、每人每天一次、全群每天几次都在「自主决策」里配。
        """

        config = self.world.decider
        gap_hours = max(0, int(getattr(config, "greet_gap_hours", 0) or 0))
        if gap_hours <= 0 or ctx.is_private:
            return
        uid = str(ctx.user_id or "")
        if not uid:
            return
        previous = float((state.user_presence.get(uid) or {}).get("last_seen") or 0.0)
        if previous <= 0:
            return  # 第一次见到他，不算"好久没来"
        gap = (float(now) - previous) / 3600.0
        if gap < gap_hours:
            return
        day = self._today_key(now)
        if str(state.greet_done.get(uid) or "") == day:
            return
        daily_max = max(0, int(getattr(config, "greet_daily_max", 0) or 0))
        if daily_max <= 0:
            return
        state.greet_pending[uid] = {
            "name": str(ctx.user_name or uid),
            "gap_hours": round(gap, 1),
            "at": float(now),
        }

    def _greet_hint(self, state: WorldState) -> str:
        """「这人好久没来了」那段提示词（读过一次就把这一笔销掉）。"""

        config = self.world.decider
        day = self._today_key(self._now())
        if str(state.greet_day or "") != day:
            state.greet_day = day
            state.greet_count = 0
        daily_max = max(0, int(getattr(config, "greet_daily_max", 0) or 0))
        if daily_max <= 0:
            state.greet_pending = {}
            return ""
        if int(state.greet_count or 0) >= daily_max:
            state.greet_pending = {}
            return ""
        picked: list[str] = []
        for uid, info in list((state.greet_pending or {}).items()):
            if len(picked) >= max(1, daily_max - int(state.greet_count or 0)):
                break
            name = str((info or {}).get("name") or uid)
            gap = float((info or {}).get("gap_hours") or 0.0)
            when = f"{gap / 24:.0f} 天" if gap >= 24 else f"{gap:.0f} 小时"
            picked.append(f"- {name}（QQ {uid}）上次露面是 {when} 前，刚回来了")
            state.greet_done[uid] = day
            state.greet_count = int(state.greet_count or 0) + 1
            state.greet_pending.pop(uid, None)
        if not picked:
            return ""
        return (
            "# 有人好久没来了\n"
            "这人好久没来了，刚在群里冒头。**可以戳一戳 ta 并打个招呼**——"
            "一句话就够，别追问人家去哪了，也别拉着一块儿聊。\n" + "\n".join(picked)
        )

    def _ask_about_hint(self, state: WorldState, ctx: MessageContext | None) -> str:
        """「你还不知道他的这些事」那段提示词。

        关系熟了之后才会出现；一次只提一件、同一件有冷却、每天有上限——
        不这么做就是查户口，不是关心。
        """

        config = self.world.profile
        if not bool(getattr(config, "ask_about_enabled", True)):
            return ""
        if ctx is None or not str(getattr(ctx, "user_id", "") or ""):
            return ""
        wanted = [str(item) for item in (getattr(config, "ask_about_fields", None) or []) if str(item)]
        if not wanted:
            return ""
        uid = str(ctx.user_id or "")
        view = self.profiles.view(self.profile_session(state, uid), uid)
        if view is None:
            return ""
        floor = max(0, int(getattr(config, "ask_about_min_level", 0) or 0))
        if int(getattr(view, "level_index", 0) or 0) < floor:
            return ""
        known = " ".join(str(item.get("text") or "") for item in (view.facts or []))
        now = self._now()
        day = self._today_key(now)
        if str(state.ask_day or "") != day:
            state.ask_day = day
            state.ask_count = 0
        room = max(0, int(getattr(config, "ask_about_daily_max", 0) or 0)) - int(
            state.ask_count or 0
        )
        if room <= 0:
            return ""
        cooldown = max(0.0, float(getattr(config, "ask_about_cooldown_days", 7) or 7)) * 86400.0
        log = dict((state.ask_log or {}).get(uid) or {})
        # 对同一个人：一天最多提一件（不然一晚上把生日年龄性别挨个问一遍，像查户口）
        person_gap = max(
            0.0, float(getattr(config, "ask_about_person_gap_hours", 24) or 24)
        ) * 3600.0
        if person_gap > 0 and now - float(log.get("_last") or 0.0) < person_gap:
            return ""
        missing = [item for item in wanted if item not in known]
        missing = [
            item for item in missing if now - float(log.get(item) or 0.0) >= cooldown
        ]
        if not missing:
            return ""
        picked = missing[0]
        log[picked] = now
        log["_last"] = now
        state.ask_log[uid] = log
        state.ask_count = int(state.ask_count or 0) + 1
        others = "、".join(missing[1:3])
        return (
            "# 你还不知道他的事\n"
            f"你到现在还不知道他的{picked}。**合适的时候自然问一句就行**（你自己找时机，"
            "别在人家正说着别的的时候硬插）。"
            + (f"另外也不知道他的{others}，以后有机会再说。" if others else "")
        )

    def _ask_about_block(self, state: WorldState) -> str:
        """「好奇心上来了，想去问问谁」那一段：只给自主决策用。

        好奇高的时候除了上网查，还可以去问人——这条补的是"搜索不是唯一出路"。
        问谁、问什么跟回话路径共用一套配额（每天几次、对同一个人隔多久、
        同一件事多久内不再问），所以好奇心不会变成查户口。

        回话路径不掺这段：别人正说着话，她突然打听第三个人的生日，那是跳戏。
        """

        config = self.world.profile
        if not bool(getattr(config, "ask_about_enabled", True)):
            return ""
        if float(state.curiosity) < ASK_ABOUT_CURIOSITY:
            return ""
        wanted = [
            str(item)
            for item in (getattr(config, "ask_about_fields", None) or [])
            if str(item)
        ]
        if not wanted:
            return ""
        now = self._now()
        day = self._today_key(now)
        if str(state.ask_day or "") != day:
            state.ask_day = day
            state.ask_count = 0
        room = max(0, int(getattr(config, "ask_about_daily_max", 0) or 0)) - int(
            state.ask_count or 0
        )
        if room <= 0:
            return ""
        floor = max(0, int(getattr(config, "ask_about_min_level", 0) or 0))
        cooldown = (
            max(0.0, float(getattr(config, "ask_about_cooldown_days", 7) or 7)) * 86400.0
        )
        person_gap = (
            max(0.0, float(getattr(config, "ask_about_person_gap_hours", 24) or 24))
            * 3600.0
        )
        # 先问"提示词里已经出现的人"：她刚看着这人说过话，顺口问一句最自然，
        # 而且「群里还有谁」那几行里已经有他的缩略版，不至于问到个她全无概念的人。
        order: list[str] = []
        for item in self.other_people(state, state.session_id):
            uid = str(item.get("user_id") or "")
            if uid and uid not in order:
                order.append(uid)
        for row in self.profile_people(state, limit=12):
            uid = str(row.get("user_id") or "")
            if uid and uid not in order:
                order.append(uid)
        for uid in order:
            view = self.profiles.view(self.profile_session(state, uid), uid)
            if view is None or bool(getattr(view, "negative", False)):
                continue
            if int(getattr(view, "level_index", 0) or 0) < floor:
                continue
            log = dict((state.ask_log or {}).get(uid) or {})
            if person_gap > 0 and now - float(log.get("_last") or 0.0) < person_gap:
                continue
            known = " ".join(str(item.get("text") or "") for item in (view.facts or []))
            missing = [
                item
                for item in wanted
                if item not in known and now - float(log.get(item) or 0.0) >= cooldown
            ]
            if not missing:
                continue
            picked = missing[0]
            log[picked] = now
            log["_last"] = now
            state.ask_log[uid] = log
            state.ask_count = int(state.ask_count or 0) + 1
            name = str(getattr(view, "name", "") or uid)
            bonds = "、".join(
                str(item) for item in (getattr(view, "affinities", None) or []) if str(item)
            )
            digest = str(getattr(view, "digest", "") or "").strip()
            who = f"- 他是谁：{name}" + (f"（{bonds}）" if bonds else "")
            if digest:
                who += f"，你记得他：{digest}"
            where = self._person_places(state, uid)
            tail = (
                f"- 想问就问一句，落点写这儿：{where}"
                if where
                else "- 想问就问一句"
            )
            others = "、".join(missing[1:3])
            return (
                "# 好奇心上来了\n"
                f"你到现在还不知道{name}的{picked}，这事最近老在你脑子里转。\n"
                f"{who}\n{tail}\n"
                "**随口一问就行，别像查户口**。"
                + (
                    f"（他的{others}你也不知道，以后有机会再说。）"
                    if others
                    else ""
                )
            )
        return ""

    def _heart_knot_block(
        self, state: WorldState, ctx: Any = None, *, autonomy: bool = False
    ) -> str:
        """「心里搁着的事」那一段：挂着、不推进，影响她说话的样子。

        心事是**分了人的**（写进来时记了 ``about``）：跟谁说话就只想谁的那件，
        不然她会把 A 的事拿到 B 面前说，还容易认错人。
        自主决策那一路把名字带上（她可能要为此去找人）。
        """

        who = str(getattr(ctx, "user_id", "") or "")
        lines: list[str] = []
        for item in list(state.heart_knots or [])[:3]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            about = str(item.get("about") or "")
            # 跟谁说话就只想谁的事；没有归属的（早期数据 / 整理时发现的）两边都带
            if who and about and about != who:
                continue
            since = float(item.get("since") or 0.0)
            days = (self._now() - since) / 86400.0 if since > 0 else 0.0
            age = f"（搁了 {days:.0f} 天了）" if days >= 1 else ""
            label = ""
            if autonomy and about and about != who:
                name = self._user_label(state, about)
                label = f"（关于 {name}）" if name else ""
            lines.append(f"- {label}{text}{age}")
        if not lines:
            return ""
        return (
            "# 你心里搁着的事\n"
            "这些不是任务、也不用去解决，只是你心里还惦记着——说话时会带出来一点："
            "要么语气淡一些，要么在合适的时机主动提一句。**别反复念叨、也别硬把话题拽过去**。\n"
            "**但他要是直接问你「怎么了 / 谁惹你了 / 还在生气吗」，就照你的口气说出来**："
            "不用一次说全，可以嘴硬、可以说一半、可以让他猜——但不能装没事，"
            "也不能只用冷淡回答他。被问到了还什么都不说，那不是你的性格。\n"
            + "\n".join(lines)
        )

    def _user_label(self, state: WorldState, user_id: str) -> str:
        """这个 QQ 号在她眼里叫什么（拿不到名字就回落到号码）。"""

        uid = str(user_id or "")
        if not uid:
            return ""
        if self.profiles.enabled():
            profile = self.profiles.profile(self.profile_session(state, uid), uid)
            if profile is not None:
                payload = dict(profile.get("payload") or {})
                names = [str(item) for item in (payload.get("names") or []) if str(item)]
                if names:
                    return names[-1]
                if str(payload.get("qq_name") or ""):
                    return str(payload["qq_name"])
        return uid
