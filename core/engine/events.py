"""事件系统：生成、推进、结算与事件日志。"""

from __future__ import annotations

from ..model_calls import model_operation

from datetime import (
    datetime,
    timedelta,
)
from typing import Any
from ..event_rhythm import TONES, in_event_hours, pick_tone
from ..events import (
    ABILITIES,
    ABILITY_LABELS,
    Decision,
    EventPackage,
    KIND_SOLO,
    MODE_IMAGINED,
    PHASE_ACT,
    SCOPE_SESSION,
    TIER_BIG,
    TIER_MICRO,
    ability_hint,
    append_step,
    apply_ability_delta,
    clamp_ability_delta,
    default_decision,
    event_from_payload,
    new_thread,
    normalize_abilities,
    open_threads,
    parse_decision,
    parse_event_package,
    parse_settlement,
    roll_check,
    situational_modifiers,
    subjective_hint,
    thread_digest,
    thread_exhausted,
    thread_step_lines,
    tier_emotion,
)
from ..json_actions import (
    PlannedAction,
    extract_json_object,
)
from ..memory import SCENE
from ..models import NodeDef
from ..mood import roll_day_mood
from ..speech_policy import speech_origin
from ..state import (
    EVENT_THREAD_KEEP,
    STATE_NAPPING,
    STATE_SLEEPING,
    STATE_WALKING,
    WorldState,
)
from .constants import EVENT_ROLL_MAX_GAP_HOURS
from .models import TickOutcome
from .util import _clip_text, ago_text


class EventsMixin:
    # ================= 事件系统 =================
    #
    # 一条线索（thread）是一串事件：初始事件 → 她怎么选 → 判定 → 结果 →（可能还有下一步）。
    # 生成和结算走打杂 / 生成器模型，只有「她怎么选」用主模型——那是角色性最强的一步。
    # 判定是纯计算，不调模型：所以这四个步骤都写得进日志，也都能单独测。

    def abilities_line(self, state: WorldState) -> str:
        """能力值那一行（人话，不是数字）。按需注入：关掉能力值就整段不出现。"""

        config = self.world.abilities
        if not bool(getattr(config, "enabled", True)):
            return ""
        values = normalize_abilities(state.abilities or config.initial)
        hints = [ability_hint(values[name]) for name in ABILITIES]
        # 四项同档是常态（初始值就一样）：逐条写「体力还算顺手、智力还算顺手、…」
        # 是同一句话说四遍，收成一句；真的分档了才逐项写。
        if len(set(hints)) == 1:
            body = "、".join(ABILITY_LABELS[name] for name in ABILITIES) + "都" + hints[0]
        else:
            body = "、".join(
                f"{ABILITY_LABELS[name]}{hint}" for name, hint in zip(ABILITIES, hints)
            )
        return (
            "# 你自己的本事\n"
            + body
            + "（这是你平时的手感，用来判断一件事对你难不难；不用说出来）"
        )

    def pending_event_line(self, state: WorldState) -> str:
        """心里挂着的事：等待协助、或者手头还有没完的线索。"""

        info = state.pending_help or {}
        stage = str(info.get("state") or "")
        title = str(info.get("title") or "那件事")
        if stage == "active":
            return (
                "# 你心里挂着的事\n"
                f"你刚开口找人拿主意（{title}），还在等人回话。你可以照常做别的事、"
                "照常接别人聊的话题；对方可能一直不回，等不到就自己拿主意。"
            )
        if stage == "idle":
            return (
                "# 你心里挂着的事\n"
                f"{title} 这件事还没解决，但你已经不催了。有人提起就接一句，"
                "没人提就先放着，别反复提。"
            )
        open_ones = [item for item in open_threads(state.event_threads) if item.get("pending_followup")]
        if open_ones:
            lines = [f"- {item.get('title') or '一件事'}：{item.get('pending_followup')}" for item in open_ones[:2]]
            return "# 手头没完的事\n" + "\n".join(lines)
        return ""

    def autonomous_prompt_note(self, state: WorldState, *, respect_pause: bool = True) -> str:
        """自主开口时那句"刚才的情况"：被冷落了就别追着说。

        以前这句话只出现在系统提示词里，位置靠后、又和一堆规则混在一起，
        模型经常当没看见。现在把它挪到用户提示词里，紧跟"现在该你说话了"。
        """

        if respect_pause and not self.proactive_chat_allowed(state.session_id):
            return "自主搭话已暂停：只安排自己的静默生活，不主动找人、插话或发送问候。日程和事件仍按各自安排执行。"
        boundary = ("\n生活分享只允许发往额外生活分享白名单；当前会话未授权时，"
                    "只过自己的生活，不汇报活动、成果、日程或心情。被问到相关事情时正常回答。\n"
                    if not self.speech_allowed(state.session_id, "life_share") else "")
        hint = self.engagement.hint(state)
        if not hint:
            return boundary
        return boundary + (
            "# 刚才的情况\n"
            f"{hint}\n"
            "（没人接的时候，下一轮**大概率就别追着说了**——除非这件事真的需要有人回应。"
            "没有新的具体联系缘由就先放下，别对着空气追问或另起生活汇报。）"
        )

    def _ensure_abilities(self, state: WorldState) -> dict[str, float]:
        """能力值兜底：新会话按配置初始化，老存档缺项按默认补齐。"""

        if not state.abilities:
            config = self.world.abilities
            state.abilities = {
                name: float(getattr(config, name, 0.6) or 0.6) for name in ABILITIES
            }
        state.abilities = normalize_abilities(state.abilities)
        return state.abilities

    def _event_time(self) -> float:
        return self._now()

    def _interrupt_pending(self) -> bool:
        """她还在生成回复时又来了一条：是**丢弃这次生成**（默认）还是并成一次回。

        开关在「说话节奏」里（``interrupt_pending``）：打开 = 新消息打断旧的，
        被丢弃的那次不算"回过话"，所以水位线不动、新那一轮仍然看得到完整上下文。
        """

        style = getattr(self.world, "reply_style", None)
        return bool(getattr(style, "interrupt_pending", True))

    def _event_gap_reason(self, state: WorldState, now: float) -> str:
        """两件事之间的最小间隔（``min_gap_minutes``）：没到点就先别出事。"""

        minutes = max(0, int(getattr(self.world.events, "min_gap_minutes", 30) or 0))
        if minutes <= 0:
            return ""
        last = float(state.last_event_started_at or 0.0)
        if last <= 0:
            return ""
        left = minutes * 60 - (float(now) - last)
        if left <= 0:
            return ""
        return f"距上一件事还不到 {minutes} 分钟"

    def _stay_up_penalty(self, state: WorldState) -> float:
        """熬夜代价的倍数：**夜里醒着**、或**为事件推迟了睡觉**，取较大值不叠加。

        睡整觉、小睡都算休息，不算熬夜；夜里时段和倍数分别在
        「作息与夜晚」和「事件」里配。
        """

        if self.world.sleep.mode == "activity":
            return 1.0
        penalty = float(getattr(self.world.events, "stay_up_penalty", 1.0) or 1.0)
        if penalty <= 1.0:
            return 1.0
        awake = str(state.state or "") not in (STATE_SLEEPING, STATE_NAPPING)
        if awake and self.world.is_night_time(self.local_now().hour):
            return penalty
        if state.stay_up_until and state.world_time <= int(state.stay_up_until):
            return penalty
        return 1.0

    def _today_key(self, now: float) -> str:
        try:
            return self.local_at(now).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""

    def _roll_day_mood(self, state: WorldState, now: float) -> bool:
        """每天掷一次"今天的基调"，同一天只掷一次。

        掷的是**曲线快慢**，不是性格：今天懒散就精力掉得慢、坐得住，
        今天黏人就更容易觉得孤单。她不会主动解释这件事，但状态页和提示词里
        都写着，免得"为什么她今天怪怪的"只能靠猜。

        返回 True 表示这一次刚好掷出了新的一天（用来记一条日志）。
        """

        if not bool(getattr(self.world.state_dynamics, "daily_mood_enabled", True)):
            state.day_mood = ""
            state.day_mood_day = ""
            return False
        day = self._today_key(now)
        if not day:
            return False
        if str(state.day_mood_day or "") == day and str(state.day_mood or ""):
            return False
        state.day_mood = roll_day_mood(self.rng)
        state.day_mood_day = day
        return True

    def _event_block_reason(
        self, state: WorldState, now: float, *, manual: bool = False
    ) -> str:
        """现在能不能出事？能就返回空串，不能就返回一句**具体**的原因。

        ``manual=True`` 是用户直接投递（指令 / 编辑器按钮）：这种情况不受「在同一个地方
        待够几分钟」的限制——那是给自动掷骰用的门槛，人都点名要一件事了就别拿它挡。
        """

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return "事件系统在全局设置里关着"
        if self._is_asleep(state) and not bool(getattr(config, "in_sleep", False)):
            return "她正在睡觉（想要梦里的剧情可以打开「睡觉时也出事」）"
        if state.state == STATE_WALKING:
            return "她正在走路"
        current = state.current_action or {}
        if str(current.get("type") or "") == "walk_to":
            return "她正在走路"
        if current and (manual or not config.while_busy):
            label = str(current.get("desc") or "").strip()
            definition = self.world.action_map().get(str(current.get("type") or ""))
            name = label or (definition.name if definition is not None else "") or str(
                current.get("type") or "手上的事"
            )
            return f"她正在「{name}」"
        if manual:
            return ""
        # 自动掷骰：得在这个地方待够一段时间，才算"在这儿生活"
        dwell = max(0, int(getattr(config, "dwell_minutes", 5) or 0)) * 60.0
        since = float(state.node_since or 0.0)
        if since <= 0:
            state.node_since = float(now)
            return "她刚到这个世界，先让她缓一会儿"
        waited = float(now) - since
        if waited < dwell:
            left = max(1, int(round((dwell - waited) / 60.0)))
            return f"她刚到这里还不到 {max(1, int(getattr(config, 'dwell_minutes', 5) or 0))} 分钟（还差约 {left} 分钟）"
        return ""

    def _event_allowed(self, state: WorldState, now: float) -> bool:
        """现在这一刻允许自动发生事件吗。"""

        return not self._event_block_reason(state, now)

    def _event_roll_gap_hours(self, state: WorldState, now: float) -> float:
        """距上次掷骰过了多久（小时），并把时钟推到现在。

        关键是**每个 tick 都推进**：以前只在"能掷骰"的时候推，睡觉 / 忙一件长动作
        期间会攒下好几个小时的"欠账"，醒来那一 tick 的概率被顶到接近 100%，
        看起来就像"刚醒就必出事"。现在过去的就让它过去。
        """

        last = float(state.event_last_roll_at or 0.0)
        state.event_last_roll_at = float(now)
        if last <= 0:
            return 0.0
        gap = max(0.0, (float(now) - last) / 3600.0)
        return min(gap, EVENT_ROLL_MAX_GAP_HOURS)

    def _roll_event_tier(
        self, state: WorldState, now: float, gap_hours: float
    ) -> tuple[str, dict[str, float]]:
        """掷骰：这一刻要不要出事、出哪一档。

        用"每小时期望次数"折算成这一段时间的概率（泊松近似），不是固定间隔——
        固定间隔会带上整点感，一眼就看出是定时器。
        """

        config = self.world.events
        dt_hours = max(0.0, float(gap_hours))
        info = {"gap_hours": round(dt_hours, 4), "chance": 0.0, "roll": 0.0}
        if dt_hours <= 0:
            return "", info
        rates = {
            "micro": max(0.0, float(getattr(config, "micro_per_hour", 1.0) or 0.0)),
            "small": max(0.0, float(getattr(config, "small_per_hour", 0.3) or 0.0)),
            "big": max(0.0, float(getattr(config, "big_per_hour", 0.02) or 0.0)),
        }
        total = sum(rates.values())
        if total <= 0:
            return "", info
        chance = min(1.0, total * dt_hours)
        roll = self.rng.random()
        info = {"gap_hours": round(dt_hours, 4), "chance": round(chance, 4), "roll": round(roll, 4)}
        if roll >= chance:
            return "", info
        point = self.rng.random() * total
        chosen = ""
        for tier in ("micro", "small", "big"):
            if rates[tier] <= 0:
                continue
            chosen = tier
            point -= rates[tier]
            if point <= 0:
                return tier, info
        return chosen, info

    def _pick_genre(self, state: WorldState) -> dict[str, Any] | None:
        """按权重抽一个事件题材（最近用过的排除，用过的还要过冷却期）。

        权重必须由代码掷：模型每次调用都是独立采样，告诉它"按 50/20/30 分配"它也不会遵守，
        只会挑自己偏好的那类写——结果就是永远只有"温馨小事"。
        """

        config = self.world.events
        entries = [
            item
            for item in list(getattr(config, "genres", []) or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        weighted: list[tuple[dict[str, Any], float]] = []
        for item in entries:
            try:
                weight = float(item.get("weight") or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            if weight > 0:
                weighted.append((item, weight))
        if not weighted:
            return None
        recent_keep = max(0, int(getattr(config, "genre_recency", 4) or 0))
        recent = {str(name) for name in list(state.event_recent_genres or [])[-recent_keep:]} if recent_keep else set()
        # 题材冷却：权重 40% 的「日常小事」光靠"排除最近 N 条"压不住，
        # 隔一件就又回来了。冷却期内直接不给权重，剩下的按原比例分。
        cooldown = max(0.0, float(getattr(config, "genre_cooldown_minutes", 0) or 0)) * 60.0
        now = self._now()
        used_at = dict(getattr(state, "event_genre_at", None) or {})

        def blocked(item: dict[str, Any]) -> bool:
            name = str(item.get("name") or "")
            if name in recent:
                return True
            if cooldown <= 0:
                return False
            stamp = float(used_at.get(name) or 0.0)
            return stamp > 0 and (now - stamp) < cooldown

        pool = [item for item in weighted if not blocked(item[0])] or weighted
        total = sum(weight for _item, weight in pool)
        point = self.rng.random() * total
        for item, weight in pool:
            point -= weight
            if point <= 0:
                return item
        return pool[-1][0]

    def _active_thread(self, state: WorldState) -> dict[str, Any] | None:
        """正在进行中的那件事（同一时间只允许一件）。

        优先返回"她正在等群友拿主意"的那条；否则返回最近开的那条未完结线索。
        """

        info = state.pending_help or {}
        if str(info.get("state") or "") in ("active", "idle"):
            found = self._thread_by_id(state, str(info.get("thread_id") or ""))
            if found is not None:
                return found
        opened = open_threads(state.event_threads)
        if not opened:
            return None
        return max(opened, key=lambda item: float(item.get("opened_at") or 0.0))

    def _thread_critical(self, thread: dict[str, Any] | None) -> bool:
        """这条事件是不是"危险 / 紧急"（日程要让路）。"""

        root = (thread or {}).get("root") or {}
        return bool(root.get("critical"))

    def _event_place(self, package: EventPackage, state: WorldState) -> tuple[str, str]:
        """这件事发生在哪：``(记忆用的地点键, 人话名字)``。

        世界里的节点事件（锅盖卡死→厨房）用节点 id；地图外的事（逛街、坐车）用
        ``outside:场景名``——**不能拿她的物理位置顶替**，否则"在窗边"会把逛街的事记进窗边，
        以后按地点根本搜不到。
        """

        node_map = self.world.node_map()
        raw = str(package.place or "").strip()
        if raw and raw in node_map:
            node = node_map[raw]
            return raw, str(package.place_name or node.name or raw)
        # 生成器给的是人话地名时，先把「外面·商业街」这种拆出来找节点
        for key, node in node_map.items():
            name = str(node.name or "")
            if raw and (raw == name or raw == key):
                return key, name or key
            if package.place_name and package.place_name == name:
                return key, name or key
        external = str(package.place_name or raw or package.scene or "").strip()
        if external:
            return f"outside:{external}", external
        # 都没有就退回她此刻所在的地方（老事件 / 池子里的条目）
        node = node_map.get(state.node_id)
        return state.node_id, str(node.name if node is not None else state.node_id)

    def _event_place_line(self, thread: dict[str, Any] | None) -> str:
        """这件事发生在哪（给提示词里那一行用）。"""

        item = thread or {}
        root = item.get("root") or {}
        for source in (root, item):
            name = str(source.get("place_name") or "").strip()
            if name:
                return name
            place = str(source.get("place") or "").strip()
            if place:
                return place.split(":", 1)[-1]
        return ""

    def _journal_time_text(self, at: float, now: float) -> str:
        """她自己的账里的时间标注：今天/昨天/前天 + HH:MM，更早写日期。"""

        try:
            moment = self.local_at(at)
            today = self.local_at(now)
        except (OverflowError, OSError, ValueError):
            return ""
        days = (today.date() - moment.date()).days
        clock = moment.strftime("%H:%M")
        if days <= 0:
            return f"今天 {clock}"
        if days == 1:
            return f"昨天 {clock}"
        if days == 2:
            return f"前天 {clock}"
        return f"{moment.strftime('%m-%d')} {clock}"

    def event_journal(self, state: WorldState, now: float | None = None) -> str:
        """她自己的账（三段），直接进提示词。

        - **我正在经历**：当前那一件事的详细处境；
        - **最近发生在我身上的事**：窗口内的全部事件，按时间从早到晚、每条一行带时间；
        - **有件事我心里还挂着**：挂起中的那件事，一行轻提示。

        末尾再补一段「你排好的日程」：每条写清是什么、上次什么时候跑的、
        下次大概什么时候（一次性日程没有"下次"）。
        """

        moment = float(now if now is not None else self._now())
        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return ""
        window = max(0.0, float(getattr(config, "recent_window_hours", 24.0) or 0.0)) * 3600.0
        limit = max(0, int(getattr(config, "recent_max_lines", 8) or 0))
        threads = [item for item in list(state.event_threads or []) if isinstance(item, dict)]
        active = self._active_thread(state)
        suspended = active is not None and self._thread_suspended(state, active, moment)
        detailed = active if (active is not None and not suspended) else None
        detailed_id = str((detailed or {}).get("id") or "")

        recent: list[dict[str, Any]] = []
        for item in threads:
            if str(item.get("id") or "") == detailed_id:
                continue
            stamp = float(item.get("closed_at") or item.get("updated_at") or item.get("opened_at") or 0.0)
            if window and moment - stamp > window:
                continue
            recent.append(item)
        recent.sort(key=lambda item: float(item.get("updated_at") or item.get("opened_at") or 0.0))
        # 条数超了：先顶掉微事件（最旧的），再顶最旧的
        if limit and len(recent) > limit:
            drop = len(recent) - limit
            light = [item for item in recent if str(item.get("tier") or "") == TIER_MICRO]
            dropped: list[str] = []
            for item in light:
                if len(dropped) >= drop:
                    break
                dropped.append(str(item.get("id") or ""))
            for item in recent:
                if len(dropped) >= drop:
                    break
                key = str(item.get("id") or "")
                if key not in dropped:
                    dropped.append(key)
            recent = [item for item in recent if str(item.get("id") or "") not in dropped]

        blocks: list[str] = []
        if detailed is not None:
            head = f"# 我正在经历（{self._journal_time_text(float(detailed.get('opened_at') or moment), moment)}"
            place = self._event_place_line(detailed)
            if place:
                head += f" · {place}"
            head += "）"
            lines = [f"{detailed.get('title') or '一件事'}：{str((detailed.get('root') or {}).get('hook') or '')}"]
            for step in thread_step_lines(detailed)[-3:]:
                lines.append(f"- {step}")
            pend = str(detailed.get("pending_followup") or "").strip()
            if pend:
                lines.append(f"- 还没完：{pend}")
            blocks.append(head + "\n" + "\n".join(lines))
        if recent:
            rows = []
            for item in recent:
                stamp = float(item.get("updated_at") or item.get("opened_at") or 0.0)
                when = self._journal_time_text(stamp, moment)
                place = self._event_place_line(item)
                title = str(item.get("title") or "").strip()
                text = str(item.get("line") or "").strip()
                if title and text and title not in text:
                    text = f"{title}：{text}"
                elif not text:
                    text = title or "一件事"
                rows.append(f"- {when}　{'（' + place + '）' if place else ''}{text}")
            blocks.append(
                "# 最近发生在我身上的事（按时间从早到晚，都是**你自己经历**的）\n"
                + "\n".join(rows)
                + "\n（提到时间时说人话：今天 / 昨天 / 前几天，别报数字）"
            )
        open_ones = [
            item
            for item in threads
            if str(item.get("status") or "open") == "open"
            and str(item.get("id") or "") != detailed_id
            and bool(getattr(config, "open_thread_hint", True))
            and (not window or moment - float(item.get("updated_at") or item.get("opened_at") or 0.0) <= window)
        ]
        if open_ones:
            rows = []
            for item in open_ones[:3]:
                place = self._event_place_line(item)
                head = str(item.get("title") or "一件事")
                if place:
                    head += f"（{place}）"
                note = str(item.get("pending_followup") or "").strip() or "还没弄清楚"
                rows.append(f"- {head}：{note}")
            blocks.append("# 有件事我心里还挂着（不用现在提，别反复念叨）\n" + "\n".join(rows))

        plan = self.schedule_journal(state, moment)
        if plan:
            blocks.append(plan)
        return "\n\n".join(blocks)


    def schedule_journal(self, state: WorldState, now: float | None = None) -> str:
        """「你排好的日程」：是什么、上次什么时候跑的、下次什么时候（一次性没有“下次”）。"""

        items = [
            item
            for item in list(getattr(self.schedules, "schedules", None) or [])
            if bool(getattr(item, "enabled", True))
        ]
        if not items:
            return ""
        moment = self.local_at(now if now is not None else self._now())
        last_map = dict(getattr(state, "schedule_last_fired", None) or {})
        rows: list[str] = []
        for item in items:
            bits: list[str] = []
            note = str(getattr(item, "note", "") or "").strip()
            when = str(getattr(item, "time", "") or "").strip()
            once = bool(getattr(item, "once", False))
            days = [str(day) for day in (getattr(item, "days", None) or [])]
            if once:
                date = str(getattr(item, "date", "") or "").strip()
                label = date or "下一次到点就跑"
                bits.append("只做这一次（" + label + " " + when + "）")
            else:
                day_text = "每天" if (not days or len(days) >= 7) else "、".join(days)
                bits.append(f"{day_text} {when}")
            if note:
                bits.append(f"做什么：{note}")
            last = float(last_map.get(str(getattr(item, "id", "") or "")) or 0.0)
            if last > 0:
                bits.append("上次：" + ago_text((float(self._now()) - last) / 60))
            else:
                bits.append("上次：还没跑过")
            if not once:
                nxt = self._next_time_text(item, moment)
                if nxt:
                    bits.append("下次：" + nxt)
            rows.append("- " + "；".join(bits))
        return "# 你排好的日程\n" + "\n".join(rows[:8])

    def _next_time_text(self, schedule: Any, moment: datetime) -> str:
        """这条日程下一次什么时候：今天/明天/N 天后 + 时间。"""

        try:
            hour, minute = (int(part) for part in str(schedule.time).split(":")[:2])
        except (TypeError, ValueError):
            return ""
        days = [str(day) for day in (schedule.days or [])]
        for offset in range(0, 8):
            when = (moment + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if when <= moment:
                continue
            if days and self.WEEKDAY_KEYS[when.weekday()] not in days:
                continue
            label = "今天" if offset == 0 else ("明天" if offset == 1 else f"{offset} 天后")
            return f"{label} {hour:02d}:{minute:02d}"
        return ""

    def _thread_suspended(
        self, state: WorldState, thread: dict[str, Any] | None, now: float
    ) -> bool:
        """这件事是不是"挂起"了：不在等群友回应、下一步还要等很久。

        挂起的那件不占「我正在经历」那一段——中间那几小时她在做饭看书，不该被它绑着。
        """

        if not isinstance(thread, dict):
            return False
        info = state.pending_help or {}
        if str(info.get("state") or "") in ("active", "idle"):
            return False  # 正在等群友回话，算"正在经历"
        when = float(thread.get("next_step_at") or 0.0)
        if when <= 0:
            return False
        minutes = max(0, int(getattr(self.world.events, "suspend_after_minutes", 30) or 0))
        if minutes <= 0:
            return False
        return (when - float(now)) > minutes * 60.0

    async def _generate_event_package(
        self,
        state: WorldState,
        node: NodeDef | None,
        *,
        tier: str,
        seed: str = "",
        thread: dict[str, Any] | None = None,
        genre: dict[str, Any] | None = None,
        tone: str = "",
    ) -> EventPackage | None:
        """让打杂 / 生成器模型现编一件（提示词是裁剪过的，不带世界规则与动作表）。"""

        config = self.world.events
        llm = self.creator_llm or self.helper_llm
        if llm is None:
            return None
        if thread:
            tone = tone or str(thread.get("tone") or "")
            if genre is None and thread.get("genre"):
                genre = {"name": thread["genre"]}
        request = self.context_request(state, state.session_id, task="event", rows=[],
            text="\n".join((seed, thread_digest(thread, complete=True) if thread else "", tone)))
        await self.prepare_context_memory(request)
        system, prompt = self.prompts.build_event_package_prompt(
            persona_brief=await self.event_persona_brief(state.session_id),
            scene_line=self.prompts.scene_compact(node),
            state_line=self._state_persona_line(state, node),
            thread_digest="",
            recent_titles=list(state.event_recent_titles or []),
            red_lines=list(getattr(config, "red_lines", []) or []),
            seed=seed,
            tier=tier,
            genre=genre,
            genre_scale=str(getattr(config, "genre_scale", "") or ""),
            tone_note=(": ".join(TONES[tone]) if tone in TONES else ""),
            extra_prompt=config.auto.extra_prompt if tone else "",
            recent_memories=[],
        )
        prompt += "\n" + await self.task_background(state, task="event", query=request.query,
            required=("# 当前事件的前因与已发生进展\n" + thread_digest(thread, complete=True)) if thread else "", request=request)
        try:
            reply = await self._ask_creator(state.session_id, system, prompt)
        except Exception as exc:
            self._log("debug", f"生成事件失败：{exc}")
            return None
        package = parse_event_package(reply or "", source="user" if seed else "llm")
        if package is None:
            return None
        package.tier = tier
        return package

    def _state_persona_line(self, state: WorldState, node: NodeDef | None) -> str:
        """给她/打杂模型看的一行状态（人话）。"""

        bits = [
            f"地点：{node.name if node else state.node_id or '未知'}",
            f"心情：{state.mood}",
            f"精力 {'不错' if state.energy > 0.6 else ('一般' if state.energy > 0.3 else '很低')}",
        ]
        current = state.current_action or {}
        if current:
            desc = str(current.get("desc") or current.get("type") or "")
            if desc:
                bits.append(f"正在做：{desc}")
        if state.storm:
            bits.append("有点上头")
        return "；".join(bits)

    async def _maybe_run_event(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        """tick 里的那一步：先推进等待中的求助，再看要不要出事。"""

        config = self.world.events
        if not bool(getattr(config, "enabled", True)):
            return
        now = self._event_time()
        self._ensure_abilities(state)
        # 掷骰时钟每 tick 都推进：过去的就让它过去，不攒"欠账"
        gap = self._event_roll_gap_hours(state, now)
        await self._advance_pending_help(state, node, outcome, now)

        # 有线索等着续：接着演下一步
        if await self._continue_thread(state, node, outcome, now):
            return

        # 还有没完的事（挂在后台、等群友回应、或者刚收尾没多久）就先不掷新的
        if self._active_thread(state) is not None:
            return

        auto = config.auto
        if not auto.enabled:
            return
        today = self.local_now().date().isoformat()
        if state.auto_event_day != today:
            state.auto_event_day = today
            state.auto_event_count = 0
        if state.auto_event_count >= auto.max_per_day:
            return
        if not in_event_hours(self.local_now().hour, auto.start_hour, auto.end_hour):
            return
        if now < state.auto_event_retry_at:
            return

        blocked = self._event_block_reason(state, now)
        if blocked:
            self._log("debug", f"这次不安排事件：{blocked}")
            return
        # 两件事之间硬性隔开：刚完结又来一件，群里看着就像在刷
        gap_blocked = self._event_gap_reason(state, now)
        if gap_blocked:
            self._log("debug", f"这次不安排事件：{gap_blocked}")
            return
        tier, roll_info = self._roll_event_tier(state, now, gap)
        if not tier:
            self._last_roll_info = roll_info
            return
        await self._start_event(
            state, node, outcome, tier=tier, source="roll", roll_info=roll_info
        )

    async def _start_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        tier: str,
        seed: str = "",
        source: str = "roll",
        thread: dict[str, Any] | None = None,
        roll_info: dict[str, float] | None = None,
    ) -> None:
        """开一件新事（或者给已有线索接上一步）。"""

        config = self.world.events
        package: EventPackage | None = None
        # 同一时间只允许一件事：有没完的就别开新的（续线走上一条）
        if thread is None:
            active = self._active_thread(state)
            if active is not None:
                self._log(
                    "debug",
                    f"手上有没完的事件（{active.get('title')}），这次不安排新的",
                )
                return
        genre: dict[str, Any] | None = None
        tone = str((thread or {}).get("tone") or "")
        if seed:
            # 用户投递的事件：照着他给的种子现编一件
            package = await self._generate_event_package(
                state, node, tier=tier, seed=seed, thread=thread, genre=None
            )
            if package is None:
                package = self._seed_package(seed, tier=tier)
        else:
            # 题材由代码按权重掷（模型只会挑自己偏好的那类写），每一件都临场生成
            # A continuation keeps the original subject and tone. Re-drawing
            # them mid-story used to turn an ordinary discovery into trouble.
            if thread and thread.get("genre"):
                genre = {"name": thread["genre"]}
            else:
                genre = self._pick_genre(state)
            if thread is None and source == "roll":
                tone = pick_tone(config.auto.tone_weights, self.rng)
            package = await self._generate_event_package(
                state, node, tier=tier, thread=thread, genre=genre, tone=tone
            )
        if package is None:
            if source == "roll" and thread is None:
                state.auto_event_failures = min(5, state.auto_event_failures + 1)
                state.auto_event_retry_at = self._event_time() + min(
                    3600, 300 * (2 ** (state.auto_event_failures - 1))
                )
            # 生成失败（没配模型 / 模型没给出可用的包）：这一轮就当没出事
            self._log("debug", "事件生成没拿到可用的包，这次跳过")
            await self._log_event(
                state,
                "event_skip",
                {"tier": tier, "note": "生成模型没给出可用的事件包"},
                outcome=outcome,
            )
            return

        if thread is None:
            place_key, place_name = self._event_place(package, state)
            package.place = place_key
            package.place_name = place_name
            thread = new_thread(package, now=self._event_time(), scope=SCOPE_SESSION)
            if source == "roll":
                state.auto_event_count += 1
                state.auto_event_failures = 0
                state.auto_event_retry_at = 0.0
            if tone:
                thread["tone"] = tone
            state.event_threads = [
                *list(state.event_threads or []),
                thread,
            ][-EVENT_THREAD_KEEP:]
            # 记下"这件事是什么时候开始的"：下一件要等 min_gap_minutes
            state.last_event_started_at = self._event_time()
        else:
            thread["root"] = package.as_payload()

        if package.title:
            state.event_recent_titles = [
                *list(state.event_recent_titles or []),
                package.title,
            ][-10:]
        genre_name = str((genre or {}).get("name") or "").strip()
        if genre_name:
            thread["genre"] = genre_name
            state.event_recent_genres = [
                *list(state.event_recent_genres or []),
                genre_name,
            ][-8:]
            # 题材冷却的账：同一个题材用完之后要过一会儿才允许再抽中
            state.event_genre_at = {
                **dict(getattr(state, "event_genre_at", None) or {}),
                genre_name: self._event_time(),
            }

        await self._log_event(
            state,
            "event",
            {
                "title": package.title,
                "hook": package.hook,
                "node": state.node_id,
                "tier": package.tier,
                "kind": package.kind,
                "imagined": package.mode == MODE_IMAGINED,
                "need_help": package.needs_help,
                "source": package.source or source,
                "thread": thread.get("id"),
                "genre": genre_name or str(thread.get("genre") or ""),
                "tone": tone,
                "critical": bool(package.critical),
                # 掷骰明细：时间差、当时的概率、掷出的值（排查"为什么突然连出两件"用）
                "roll": dict(roll_info or {}),
            },
            outcome=outcome,
        )

        if package.tier == TIER_MICRO or not package.options:
            await self._settle_micro(state, outcome, thread, package)
            return
        await self._run_event_step(state, node, outcome, thread, package)

    def _seed_package(self, seed: str, *, tier: str = "small") -> EventPackage:
        """用户投递的种子没有模型可用时的兜底：给三个通用选项。"""

        text = " ".join(str(seed or "").split())
        return event_from_payload(
            {
                "title": text[:16],
                "scene": "",
                "hook": text,
                "tier": tier,
                "kind": KIND_SOLO,
                "options": [
                    {"desc": "硬着头皮上", "abilities": ["composure"]},
                    {"desc": "换个更稳的办法", "abilities": ["wits"]},
                    {"desc": "先放一放", "no_check": True},
                ],
            },
            source="user",
        ) or EventPackage(title=text[:16], hook=text, tier=tier)

    def _cached_decision(self, thread: dict[str, Any]) -> Decision | None:
        """把线索里存的那次抉择还原出来（超时自己收尾时要用）。"""

        raw = thread.get("decision") if isinstance(thread, dict) else None
        if not isinstance(raw, dict):
            return None
        return Decision(
            pick=int(raw.get("pick") or -1),
            desc=str(raw.get("desc") or ""),
            abilities=[str(name) for name in list(raw.get("abilities") or [])],
            no_check=bool(raw.get("no_check")),
            reason=str(raw.get("reason") or ""),
            say=dict(raw.get("say") or {}),
            send_to=str(raw.get("send_to") or ""),
        )

    @model_operation("event", "事件推进到需要回应的一幕")
    async def _run_event_step(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        *,
        second_round: bool = False,
        suggestions: Any = None,
        recap: bool = False,
    ) -> None:
        """一步事件：她抉择 →（求助 or 判定）→ 结算。"""

        if not (self._is_asleep(state) and self.world.events.in_sleep):
            admission = self.admit_life(state, "event", critical=self._thread_critical(thread))
            if not admission["allow"]:
                return
        options = self._render_options(state, package)
        scene_note = ""
        if recap:
            place = str(thread.get("place_name") or "")
            scene_note = (
                f"这件事发生在{('「' + place + '」') if place else '别处'}，"
                "你现在人在别的地方、只是又想起了它——不要说成自己此刻就在那里，"
                "也别描述现在的环境是那个地方。"
            )
        action_defs = self._event_action_defs(state, thread)
        action_lines = self.event_action_lines(action_defs)
        # 「先去做点什么」那一轮的预算：她可以连着查几次，查完就得拿主意
        rounds_left = self._event_action_budget(state, thread)
        decision = default_decision(package)
        while True:
            allow_act = bool(action_lines) and rounds_left > 0 and not second_round
            system, prompt = self.prompts.build_event_decision_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=package.title,
                hook=package.hook,
                options=options,
                state_line=self._state_persona_line(state, node),
                ability_line=self.abilities_line(state),
                suggestions=suggestions.prompt_block() if suggestions is not None else "",
                second_round=second_round,
                scene_note=scene_note,
                action_lines=action_lines if allow_act else "",
                observations=self._event_observation_text(thread),
                act_rounds_left=rounds_left,
                session_directory=self.session_directory(state),
            )
            prompt += self.conversation_scene_hint(state, outcome.home())
            prompt += "\n" + await self.task_background(state, task="event", query=package.title + "\n" + package.hook,
                required="# 当前事件的前因与已发生进展\n" + thread_digest(thread, complete=True))
            prompt += ("\n要说的话要符合人设和此刻场合。求助只问清楚这一件具体的事，"
                       "不要点名催促、重复征集或逐条播报；没必要分享时 say 各项可以留空数组。")
            reply = await self._ask_llm(state.session_id, system, prompt)
            outcome.llm_calls += 1
            parsed = parse_decision(
                reply or "",
                package,
                allowed_actions={item.id for item in action_defs} if allow_act else set(),
            )
            decision = parsed or default_decision(package)
            if decision.phase != PHASE_ACT or not decision.actions or not allow_act:
                break
            await self._run_event_action(state, node, outcome, thread, package, decision)
            rounds_left -= 1
        # 这件事她想说给谁听：她自己挑，挑不出来就留在原来的落点上
        raw_target = str(decision.send_to or "").strip()
        if raw_target:
            picked = self.resolve_send_to(state, raw_target, fallback="")
            if picked:
                outcome.session_id = picked
            else:
                outcome.notes.append(
                    f"她本来想跟「{raw_target}」说这件事，但那儿不在她能说话的地方里"
                )
        thread["decision"] = {
            "pick": decision.pick,
            "desc": decision.desc,
            "abilities": list(decision.abilities),
            "no_check": bool(decision.no_check),
            "reason": decision.reason,
            "say": dict(decision.say),
            "send_to": decision.send_to,
        }
        await self._log_event(
            state,
            "event_choice",
            {
                "title": package.title,
                "desc": decision.desc,
                "abilities": "、".join(
                    ABILITY_LABELS.get(name, name) for name in decision.abilities
                ),
                "reason": decision.reason,
            },
            outcome=outcome,
        )

        want_help = (
            decision.ask_help
            and not second_round
            and bool(getattr(self.world.events, "intervene_enabled", True))
            and package.tier != TIER_MICRO
        ) or package.needs_help and not second_round
        if want_help:
            blocked = self._help_block_reason(state)
            if blocked:
                # 群里没人接话、或者她正在睡：这句话发出去只会显得莫名其妙，
                # 直接让她自己拿主意（结果照样进记忆和日志）。
                await self._log_event(
                    state,
                    "help",
                    {"title": package.title, "stage": "alone", "note": blocked},
                    outcome=outcome,
                )
                outcome.notes.append(f"没开口求助：{blocked}")
                # 那几句是"求助的话"，不问了就别再拿去当结果说
                # （`Decision.lines` 在没写 success/fail 时会退回 plan）
                decision.say.pop("plan", None)
                want_help = False
        if want_help:
            await self._ask_for_help(state, node, outcome, thread, package, decision)
            return
        await self._resolve_event_step(
            state, node, outcome, thread, package, decision, suggestions=suggestions
        )

    def _render_options(self, state: WorldState, package: EventPackage) -> list[str]:
        """把选项渲染成"带她的主观判断"的人话。不给概率数字。"""

        labels = ("A", "B", "C", "D")
        lines: list[str] = []
        for index, option in enumerate(package.options[:4]):
            abilities = [name for name in option.abilities if name in ABILITIES]
            ability_value = 0.6
            if abilities:
                values = normalize_abilities(state.abilities)
                ability_value = sum(values.get(name, 0.6) for name in abilities) / len(abilities)
            difficulty = max(0.05, min(0.95, float(package.difficulty) + float(option.difficulty)))
            head = f"{index + 1}. {option.desc}"
            if option.no_check:
                lines.append(f"{head}（稳，但这件事就算了）")
                continue
            probability = ability_value * difficulty
            hint = subjective_hint(
                probability,
                affect=state.affect,
                energy=state.energy,
                composure=normalize_abilities(state.abilities).get("composure", 0.6),
            )
            names = "、".join(ABILITY_LABELS.get(name, name) for name in abilities)
            lines.append(f"{head}（{hint}，靠{names or '心性'}）" if abilities else f"{head}（{hint}）")
        if not lines:
            lines = ["1. 硬着头皮上（差不多）", "2. 先放一放（稳）"]
        return lines

    async def _ask_for_help(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
    ) -> None:
        """她开口求助：发出去（可以分几条），然后进入活跃等待。"""

        now = self._event_time()
        config = self.world.events
        lines = [str(item.get("text") or "") for item in decision.lines("plan")]
        lines = [text for text in lines if text.strip()]
        if not lines:
            lines = await self._compose_help_lines(state, package)
        lines = lines[: self._event_say_limit("ask")]
        state.pending_help = {
            "thread_id": thread.get("id"),
            "title": package.title,
            "state": "active",
            "asked_at": float(now),
            "active_until": float(now) + max(30.0, float(getattr(config, "active_seconds", 180.0))),
            "idle_until": 0.0,
            "reminder_sent": False,
            "suggestions": [],
            # 她当时是怎么问的：等没人接、要写那句自我圆场时得知道上文
            "asked": " / ".join(lines)[:120],
        }
        for line in lines:
            outcome.add_speech(line, kind="event")
        await self._log_event(
            state,
            "help",
            {
                "title": package.title,
                "stage": "ask",
                "text": " / ".join(lines),
                "wait_seconds": int(getattr(config, "active_seconds", 180.0)),
            },
            outcome=outcome,
        )
        state.add_event("event_help", {"title": package.title})

    @model_operation("event", "事件需要向对方求助")
    async def _compose_help_lines(
        self, state: WorldState, package: EventPackage
    ) -> list[str]:
        """求助那两句让她自己说；模型不可用时才用一句兜底。

        以前是代码拼死的一句「有人吗」：不管遇上什么都一样，群里看着像机器人。
        """

        helper = self.helper_llm or self.llm
        hook = _clip_text(package.hook, 60).rstrip("。")
        fallback = [f"{hook}……这可怎么办" if hook else "遇到点事……"]
        if helper is None:
            return fallback
        try:
            system, prompt = self.prompts.build_help_ask_prompt(
                persona_text=await self._persona_text(state.session_id),
                title=package.title,
                hook=package.hook,
            )
        except Exception:
            return fallback
        prompt += "\n" + await self.task_background(state, task="expression", query=package.title + "\n" + package.hook)
        reply = await self._ask_helper(state.session_id, system, prompt)
        if not reply:
            return fallback
        lines: list[str] = []
        payload = extract_json_object(reply)
        if isinstance(payload, dict):
            raw = payload.get("lines")
            if isinstance(raw, str):
                raw = [raw]
            lines = [
                " ".join(str(item).split())
                for item in list(raw or [])
                if str(item).strip()
            ]
            # 返回了 JSON 但没有 lines：不要把她自己的乱 JSON 当话发出去
            return [item[:60] for item in lines if item][:2] or fallback
        lines = [" ".join(item.split()) for item in str(reply).splitlines() if item.strip()]
        lines = [item[:60] for item in lines if item][:2]
        return lines or fallback

    def _max_say_lines(self) -> int:
        """事件里她一次最多说几句。

        求助那种"有人吗 / 我这是… / 怎么办啊"要分几条发，所以给得比平时宽松；
        但绝对硬顶拦着，不会刷屏。``kind``：``ask`` 求助 / ``result`` 结果 / ``beat`` 心跳更新。
        """

        return self._event_say_limit("ask")

    def _help_block_reason(self, state: WorldState) -> str:
        """现在不适合开口求助的原因（空串 = 可以喊）。

        群里没人接话时喊「有人吗」只有一种结果：她在那儿干等，群友看到一句
        没头没尾的话——比她自己把事情办了还奇怪。
        """

        if self._is_asleep(state):
            return "她在睡觉"
        if not self.group_is_alive(state):
            return "群里最近没人说话"
        return ""

    def _event_public_speech(self, state: WorldState) -> bool:
        """事件里的话要不要发到群里。睡着（打盹也算）时只记状态和记忆。"""

        return not self._is_asleep(state)

    def _event_say_limit(self, kind: str = "ask") -> int:
        config = self.world.events
        try:
            base = int(getattr(config, "max_say_lines", 4) or 4)
        except (TypeError, ValueError):
            base = 4
        try:
            hard = int(getattr(config, "max_say_lines_hard", 6) or 6)
        except (TypeError, ValueError):
            hard = 6
        limit = max(1, min(base, hard))
        if kind == "result":
            limit = min(limit, 2)
        elif kind == "beat":
            limit = 1
        return limit

    async def _resolve_event_step(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
        decision: Decision,
        *,
        suggestions: Any = None,
        forced: bool = False,
    ) -> None:
        """判定 + 结算 + 她的话。"""

        now = self._event_time()
        config = self.world.events
        values = normalize_abilities(state.abilities)
        ability = (decision.abilities or ["wits"])[0]
        ability_value = values.get(ability, 0.6)
        difficulty = max(
            0.05, min(0.95, float(package.difficulty) + float(decision.difficulty_delta))
        )
        if decision.no_check:
            # 她选了「先不做」：不掷骰，也不长能力值
            from ..events import CheckResult, TIER_SKIP

            modifiers: list[tuple[str, float]] = []
            check = CheckResult(
                ability=ability,
                ability_value=ability_value,
                difficulty=difficulty,
                tier=TIER_SKIP,
            )
        else:
            modifiers = situational_modifiers(
                energy=state.energy,
                affect=state.affect,
                alignment=int(getattr(suggestions, "alignment", 0) or 0)
                if bool(getattr(config, "suggest_tools", True))
                else 0,
                failed_before=any(
                    str(step.get("tier") or "") == "fail"
                    for step in list(thread.get("steps") or [])
                ),
            )
            check = roll_check(
                ability=ability,
                ability_value=ability_value,
                difficulty=difficulty,
                modifiers=modifiers,
                roll=self.rng.random,
            )
        await self._log_event(
            state,
            "event_check",
            {
                "title": package.title,
                "ability": ABILITY_LABELS.get(ability, ability),
                "ability_value": round(ability_value, 3),
                "difficulty": round(difficulty, 3),
                "probability": check.probability,
                "roll": check.roll,
                "tier": check.label,
                "skip": bool(decision.no_check),
                "modifiers": [
                    {"label": label, "factor": factor} for label, factor in modifiers
                ],
            },
            outcome=outcome,
        )

        settlement = await self._settle_event(
            state,
            node,
            package,
            decision,
            check=check,
            suggestions=suggestions,
            thread=thread,
        )
        await self.link_event_matter(state, thread, settlement.life_matter)
        delta, spent = clamp_ability_delta(
            settlement.ability_delta,
            spent_today=state.ability_spent_today,
            day=state.ability_day,
            today=self._today_key(now),
        )
        if delta:
            state.abilities = apply_ability_delta(state.abilities, delta)
        state.ability_day = self._today_key(now)
        state.ability_spent_today = spent
        if settlement.state_delta:
            self.dynamics.apply_pulse(
                state,
                settlement.state_delta,
                now=now,
                cause=f"刚才那件事（{package.title}）",
            )
        self._apply_event_life(state, check.tier, settlement.state_delta)
        # 判定档位本身也带一点情绪：成功偏正面、失败偏负面（可关）
        if bool(getattr(config, "result_emotion", True)):
            pulse = tier_emotion(check.tier)
            if pulse:
                self.dynamics.apply_pulse(
                    state,
                    pulse,
                    now=now,
                    cause=f"刚才那件事（{package.title}）",
                )
                state.mood = self.dynamics.derive_mood(state)
        if settlement.outcome:
            keep = max(1, int(getattr(config, "event_digest_lines", 12) or 12))
            state.event_digest = [
                *list(state.event_digest or []),
                f"{package.title}：{settlement.outcome}",
            ][-keep:]
        if settlement.memory or settlement.outcome:
            # 记忆记在**事件发生的地点**，不是她此刻的位置：
            # 否则"在窗边"会把逛街被跟的事记进窗边，以后按地点根本搜不到。
            place_key, place_name = self._event_place(package, state)
            content = str(settlement.memory or settlement.outcome)[:120]
            if place_name and place_name not in content:
                content = f"在{place_name}：{content}"[:120]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=place_key,
                content=content,
                memory_type=SCENE,
                emotion=state.mood,
                weight=0.4,
                affect=state.affect,
                valence=state.valence,
            )

        followup = str(settlement.followup or "").strip()
        exhausted = thread_exhausted(
            thread,
            now=now,
            max_steps=int(getattr(config, "max_steps", 6) or 6),
            max_minutes=float(getattr(config, "max_minutes", 120.0) or 120.0),
        )
        if exhausted:
            followup = ""
        elif not followup and self._event_must_continue(thread, tier=package.tier):
            # 大事件不许一步收尾：模型忘了留伏笔时由代码补一步，
            # 否则"有人找我麻烦"下一句就变成"我自己解决了"，看着特别潦草。
            followup = (
                f"{package.title}还没完，下一步得接着处理"
            )
        # 这一幕能不能说出口：**必须在 append_step 之前算**——
        # 那之后步数就变成"下一幕"了，开会话的判定会整体错开一幕。
        share_allowed = self._event_share_allows(
            state, tier=package.tier, thread=thread, followup=followup
        )
        share_step = self._event_step_no(thread)
        # 下一幕隔多久：模型可以在结算里自己定（``next_gap``，分钟），
        # 配置里的 step_gap_seconds 既是最小值也是它的兜底。
        base_gap = max(0.0, float(getattr(config, "step_gap_seconds", 60.0) or 60.0))
        if str(package.tier or "").strip().lower() == TIER_BIG:
            # 大事件要演两幕以上，间隔太密会看着像刷屏
            base_gap = max(
                base_gap,
                max(0.0, float(getattr(config, "big_step_gap_minutes", 0.0) or 0.0)) * 60.0,
            )
        asked_gap = float(getattr(settlement, "next_gap_minutes", 0.0) or 0.0) * 60.0
        next_gap = max(base_gap, min(asked_gap, 360.0 * 60.0)) if asked_gap else base_gap
        append_step(
            thread,
            outcome={
                "status": "open" if followup else "closed",
                "followup": followup,
                "next_step_gap": next_gap,
            },
            now=now,
            step={
                "desc": decision.desc,
                "ability": ABILITY_LABELS.get(ability, ability),
                "tier": check.tier,
                "tier_label": check.label,
                "result": settlement.outcome,
                "ability_delta": dict(delta),
                "suggestions": int(getattr(suggestions, "alignment", 0) or 0),
            },
        )
        if not followup:
            thread["status"] = "closed"
            thread["closed_at"] = float(now)
        # 她自己的账：每一幕的结果并进这一条（不是每幕一条，免得读起来像流水账）
        line = str(settlement.memory or settlement.outcome or "").strip()
        if line:
            old = str(thread.get("line") or "").strip()
            thread["line"] = (f"{old}；{line}" if old else line)[:220]

        slot = "success" if check.ok else "fail"
        said = [str(item.get("text") or "") for item in decision.lines(slot)]
        said = [text for text in said if text.strip()][: self._event_say_limit("result")]
        # 先记进留档，再决定这一句能不能说出口：静默的幕也该留下"她经历过"的痕迹
        self._note_event_in_chat(state, package, settlement, thread, followup=followup)
        if said and not share_allowed:
            # 睡着（或这一档本来就是静默推演）时事件照演，但不在群里出声——
            # 群友只会看到"她说自己在睡觉，却在群里讲话"。
            outcome.notes.append(f"这一幕按策略没有发到群里（{package.title}）")
            await self._log_event(
                state,
                "event_mute",
                {
                    "title": package.title,
                    "tier": package.tier,
                    "share": self._event_share_mode(package.tier),
                    "step": share_step,
                    "text": " / ".join(said)[:160],
                },
                outcome=outcome,
            )
            said = []
        if said:
            for line in said:
                outcome.add_speech(line, kind="event")
        await self._log_event(
            state,
            "event_result",
            {
                "title": package.title,
                "outcome": settlement.outcome,
                "tier": check.label,
                "ability_delta": {
                    ABILITY_LABELS.get(name, name): round(value, 3)
                    for name, value in delta.items()
                },
                "followup": followup,
                "closed": not followup,
                "suggestions": getattr(suggestions, "digest", lambda: "")()
                if suggestions is not None
                else "",
            },
            outcome=outcome,
        )
        await self._maybe_photo_for_event(state, node, outcome, thread, package)

    def _photo_action_id(self) -> str:
        """事件出图用哪个动作：按配置里的顺序挑第一个启用、且带指令的。"""

        for name in list(getattr(self.world.events, "photo_actions", []) or []):
            action = self.world.action_map().get(str(name).strip())
            if action is None or not action.enabled:
                continue
            if str(getattr(action, "llm_level", "")) == "command" and not str(
                getattr(action, "trigger_command", "") or ""
            ).strip():
                continue
            return action.id
        return ""

    async def photo_now(
        self,
        state: WorldState,
        *,
        intent: str = "",
        reason: str = "",
    ) -> str:
        """现在让她拍一张：走「事件出图」那条路（按配置挑一个**已启用**的内置拍照动作）。

        给扩展用的公开接口（H 那一套的「本场发送照片的概率」掷中就调它）。这里
        **不看**出图指令在不在：指令型动作自己会把指令交给 AstrBot 去执行，没装
        对应插件时它会记一条"没有可用的指令通道"——这条路照样算走通，等装了出图
        插件就真能出图。返回用了哪个动作 id；``""`` = 没有可用的拍照动作。
        """

        action_id = self._photo_action_id()
        if not action_id:
            return ""
        text = str(intent or "").strip() or "拍一张现在的样子，画面里带上此刻的环境。"
        planned = PlannedAction(type=action_id, intent=text, content=text)
        outcome = TickOutcome(state.session_id)
        node = self.node(state.node_id) or self.node(self.default_node_id())
        await self._execute_actions(
            state, node, outcome, [planned], depth=0, autonomous=True
        )
        await self._log_event(
            state,
            "photo",
            {
                "action": action_id,
                "why": str(reason or "有人让她拍的").strip(),
                "notes": [str(item) for item in list(outcome.notes)[:4]],
            },
        )
        return action_id

    @speech_origin("event")
    async def _maybe_photo_for_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
    ) -> None:
        """事件每一幕推进 / 完结时，按概率让她拍一张（图会带上这件事的内容）。

        出图动作自己会说话（``on_complete=llm_followup``），所以这里只负责
        把"现在发生了什么"写进意图，让图跟剧情对得上。
        """

        config = self.world.events
        try:
            chance = float(getattr(config, "photo_chance", 0.0) or 0.0)
        except (TypeError, ValueError):
            chance = 0.0
        if chance <= 0:
            return
        if self.rng.random() >= min(1.0, chance):
            return
        action_id = self._photo_action_id()
        if not action_id:
            return
        steps = [item for item in list(thread.get("steps") or []) if isinstance(item, dict)]
        last = steps[-1] if steps else {}
        bits = [
            f"你刚经历的事：{package.title}",
            f"发生了什么：{package.hook}",
        ]
        if last.get("result"):
            bits.append(f"结果：{last.get('result')}")
        bits.append("拍一张和这件事对得上的照片，意图里写清画面内容与此刻的环境。")
        intent = "；".join(str(item) for item in bits if str(item).strip())
        planned = PlannedAction(type=action_id, intent=intent, content=intent)
        await self._execute_actions(
            state, node, outcome, [planned], depth=0, autonomous=True
        )
        await self._log_event(
            state,
            "event_action",
            {
                "title": package.title,
                "action": action_id,
                "intent": intent,
                "note": "这一件事推进时顺手拍了张照",
            },
            outcome=outcome,
        )

    async def _settle_micro(
        self,
        state: WorldState,
        outcome: TickOutcome,
        thread: dict[str, Any],
        package: EventPackage,
    ) -> None:
        """微事件：生成时就带好了结果与效果，这里只落账，**不再调模型**。"""

        now = self._event_time()
        delta, spent = clamp_ability_delta(
            package.abilities,
            spent_today=state.ability_spent_today,
            day=state.ability_day,
            today=self._today_key(now),
        )
        if delta:
            state.abilities = apply_ability_delta(state.abilities, delta)
        state.ability_day = self._today_key(now)
        state.ability_spent_today = spent
        if package.effects:
            self.dynamics.apply_pulse(
                state,
                package.effects,
                now=now,
                cause=f"刚才那件小事（{package.title}）",
            )
        result = package.outcome or package.hook
        self._apply_event_life(state, str(thread.get("tone") or "daily"), package.effects)
        if result:
            # 微事件群里不主动说，但会进"这段时间发生的事"，动作完成续说时顺口带一句
            keep = max(1, int(getattr(self.world.events, "event_digest_lines", 12) or 12))
            state.event_digest = [
                *list(state.event_digest or []),
                f"{package.title}：{result}",
            ][-keep:]
        if package.memory or result:
            place_key, place_name = self._event_place(package, state)
            content = str(package.memory or result)[:120]
            if place_name and place_name not in content:
                content = f"在{place_name}：{content}"[:120]
            self.memory.remember(
                session_id=state.session_id,
                persona_id="",
                node_id=place_key,
                content=content,
                memory_type=SCENE,
                emotion=state.mood,
                weight=0.25,
                affect=state.affect,
                valence=state.valence,
            )
        append_step(
            thread,
            outcome={"status": "closed"},
            now=now,
            step={"desc": "（没有选择）", "result": result, "ability_delta": dict(delta)},
        )
        thread["status"] = "closed"
        thread["closed_at"] = float(now)
        line = str(package.memory or result or "").strip()
        if line:
            old = str(thread.get("line") or "").strip()
            thread["line"] = (f"{old}；{line}" if old else line)[:220]
        await self._log_event(
            state,
            "event_result",
            {
                "title": package.title,
                "outcome": result,
                "closed": True,
                "ability_delta": {
                    ABILITY_LABELS.get(name, name): round(value, 3)
                    for name, value in delta.items()
                },
            },
            outcome=outcome,
        )

    @model_operation("event", "事件结束，生成结算与影响")
    async def _settle_event(
        self,
        state: WorldState,
        node: NodeDef | None,
        package: EventPackage,
        decision: Decision,
        *,
        check: Any,
        suggestions: Any = None,
        thread: dict[str, Any] | None = None,
    ):
        """让事件模型写结果；它给不出东西就用事件包里的伏笔兜底。"""

        config = self.world.events
        system, prompt = self.prompts.build_event_settle_prompt(
            title=package.title,
            hook=package.hook,
            desc=decision.desc,
            tier_label=check.label,
            state_line=self._state_persona_line(state, node),
            suggestions_digest=getattr(suggestions, "digest", lambda: "")()
            if suggestions is not None
            else "",
            next_step_hint=str((thread or {}).get("pending_followup") or ""),
            fail_menu=list(getattr(config, "fail_result_menu", []) or []),
            must_continue=self._event_must_continue(thread, tier=package.tier),
        )
        raw = None
        if bool(getattr(config, "enabled", True)):
            prompt += "\n# 原有人设（始终优先）\n" + (await self._persona_text(state.session_id))[:6000]
            prompt += "\n" + await self.task_background(state, task="event", query=package.title + "\n" + package.hook,
                required=("# 结算依据：当前事件的前因与已发生进展\n" + thread_digest(thread, complete=True)) if thread else "", evidence=True)
            system += ('\n可选字段 life_matter：仅当这次经历确实让她想在以后持续做一件符合人设的小事时，'
                       '提供 {"text":"想做什么","reason":"这次经历为什么引出它","matter_id":"已有事项ID或空"}。'
                       '关联已有事项时只填有效ID及reason；没有必要就省略。'
                       '普通经历可以自然结束，不要把每件事变成待办；不得凭此建立对现实用户的新承诺、关系或身世。')
            raw = await self._ask_event(state.session_id, system, prompt)
            self._count_tool_param(state)
        settlement = parse_settlement(raw or "") if raw else None
        if settlement is None:
            fallback = (
                package.followup.get("success" if check.ok else "fail")
                or package.followup.get("success")
                or f"{package.title}这件事就这样过去了"
            )
            from ..events import Settlement

            settlement = Settlement(outcome=str(fallback)[:200])
        if not settlement.outcome:
            settlement.outcome = f"{package.title}这件事讲完了"
        # 失败涨经验：模型忘了给变化时补一点，保证"失败了也能再试"
        if not check.ok and not settlement.ability_delta:
            settlement.ability_delta = {str(decision.abilities[0] if decision.abilities else "composure"): 0.02}
        return settlement

    def _thread_by_id(self, state: WorldState, thread_id: str) -> dict[str, Any] | None:
        wanted = str(thread_id or "")
        if not wanted:
            return None
        for item in list(state.event_threads or []):
            if isinstance(item, dict) and str(item.get("id") or "") == wanted:
                return item
        return None
