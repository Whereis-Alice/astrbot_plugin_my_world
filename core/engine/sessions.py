"""会话白名单、会话组与作用域。"""

from __future__ import annotations

from ..model_calls import model_operation

from typing import Any
from ..models import (
    NodeDef,
    SessionDef,
)
from ..state import WorldState
from .util import _clip_text


class SessionsMixin:
    # ================= 会话 =================

    def is_enabled(self, session_id: str) -> bool:
        if not session_id:
            return False
        session = self._sessions_index.get(session_id)
        if session is None or not session.enabled:
            return False
        blocked = set(self.world.content_safety.session_blocklist or [])
        return session_id not in blocked

    def enabled_session_ids(self) -> list[str]:
        return [item.session_id for item in self.sessions.sessions if self.is_enabled(item.session_id)]

    def session_config(self, session_id: str) -> SessionDef | None:
        return self._sessions_index.get(session_id)

    # ---------------- 会话组 ----------------

    def group_of(self, session_id: str) -> Any | None:
        """这个会话属于哪个组（没分组返回 None）。"""

        group_id = self._group_of.get(str(session_id or ""))
        return self._group_index.get(group_id) if group_id else None

    def member_sessions(self, session_id: str) -> list[str]:
        """同组的其它会话（不含自己）。"""

        group = self.group_of(session_id)
        if group is None:
            return []
        return [str(item) for item in (group.sessions or []) if str(item) != str(session_id)]

    def group_by_id(self, group_id: str) -> Any | None:
        """按组 id 找组（编辑器选的可能就是组）。"""

        return self._group_index.get(str(group_id or ""))

    def scope_session(self, value: str) -> str:
        """编辑器里的选择换成"真正要操作的会话"。

        选的可能是会话、也可能是会话组；是组就落到它的代表会话上
        （她的状态、日志、日程都挂在那儿）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        if text in self._group_index:
            group = self._group_index[text]
            members = [str(item) for item in (group.sessions or [])]
            if not members:
                return text
            main = str(group.main_session or members[0])
            if self.is_enabled(main):
                return main
            for member in members:
                if self.is_enabled(member):
                    return member
            return main
        return text

    def group_sessions(self, session_id: str) -> list[str]:
        """同组的全部会话（含自己；没分组就只有自己）。"""

        group = self.group_of(session_id)
        if group is None:
            return [str(session_id)]
        members = [str(item) for item in (group.sessions or [])]
        return members or [str(session_id)]

    def scope_id(self, session_id: str) -> str:
        """共享作用域的 id：分过组就是组 id，记忆和聊天上下文都按它存。"""

        group = self.group_of(session_id)
        return str(group.id) if group is not None else str(session_id)

    def main_session(self, session_id: str) -> str:
        """她这一组的"主会话"；没分组就是它自己。"""

        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        return str(group.main_session or session_id)

    def state_key(self, session_id: str) -> str:
        """她的状态存在哪个会话名下。

        分过组就是同一个她：位置、数值、计划、事件都存一份，几个群 / 私聊共用。
        主会话要是被停用了，就退到组里第一个可用的成员，免得她"住在"一个不响应的会话里。
        """

        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        main = str(group.main_session or session_id)
        if self.is_enabled(main):
            return main
        for member in group.sessions or []:
            if self.is_enabled(str(member)):
                return str(member)
        return main

    def tick_session_ids(self) -> list[str]:
        """真正要推进时钟的会话：同一个她只推一次（一个组一份）。"""

        picked: list[str] = []
        seen: set[str] = set()
        for session_id in self.enabled_session_ids():
            key = self.state_key(session_id)
            if key in seen:
                continue
            seen.add(key)
            picked.append(key)
        return picked

    @staticmethod
    def dedupe_session_refs(values: Any) -> list[str]:
        """一串落点说法去掉空值与重复（顺序保持）。"""

        picked: list[str] = []
        for item in list(values or []):
            text = str(item or "").strip()
            if text and text not in picked:
                picked.append(text)
        return picked

    @classmethod
    def schedule_session_ids(cls, schedule: Any) -> list[str]:
        """日程的 ``sessions`` 摊平成一串会话 / 会话组 id（去重、去掉空值）。

        既认日程对象（``ScheduleDef``），也认配置里那条还没解析成对象的 dict
        ——保存前的归一化拿到的就是 dict。
        """

        if isinstance(schedule, dict):
            raw = schedule.get("sessions")
        else:
            raw = getattr(schedule, "sessions", None)
        return cls.dedupe_session_refs(raw or [])

    def canonical_schedule_sessions(self, sessions: Any) -> list[str]:
        """日程落点归一化：**组 id 与具体会话不共存**（跟前端选择器同一条规则）。

        前端（``pages/world_editor/app.js`` 的日程落点选择器）把两者做成了互斥：
        勾了组就只存组 id，否则只存具体会话。可旧存档 / 手工改过的配置文件里
        两种 id 会同时躺着，运行时读到的就是一份自相矛盾的落点。统一在这里收口：
        只要含组 id，就只保留组 id（组 = 让大模型在组内挑一处，具体会话作废）。

        要认得出"哪个 id 是会话组"，所以这里是实例方法（``group_by_id``）。
        """

        picked = self.dedupe_session_refs(sessions)
        groups = [item for item in picked if self.group_by_id(item) is not None]
        return groups or picked

    def schedule_sessions_of(self, schedule: Any) -> list[str]:
        """这条日程实际生效的落点：过一遍归一化。"""

        return self.canonical_schedule_sessions(self.schedule_session_ids(schedule))

    def normalize_schedule_sessions(self, schedules: Any) -> tuple[Any, list[str]]:
        """保存前把旧存档里的混合落点洗成一种，并给出人话提示。

        返回 ``(洗好的数据, 提示)``——提示会并进接口的 ``warnings`` 交回给编辑器。
        一条都不需要洗时原样返回（连 dict 都不重建），不制造无谓的 diff。
        """

        items = (schedules or {}).get("schedules") if isinstance(schedules, dict) else None
        if not isinstance(items, list):
            return schedules, []
        warnings: list[str] = []
        cleaned: list[Any] = []
        changed = False
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                cleaned.append(item)
                continue
            raw_picked = self.schedule_session_ids(item)
            kept = self.canonical_schedule_sessions(raw_picked)
            if kept == raw_picked:
                cleaned.append(item)
                continue
            changed = True
            schedule_id = str(item.get("id") or "第 %d 条" % (index + 1))
            try:
                labels = "、".join(
                    str(self.group_by_id(ref).name or ref)
                    if self.group_by_id(ref) is not None
                    else ref
                    for ref in kept[:4]
                )
            except Exception:
                labels = "、".join(kept[:4])
            warnings.append(
                f"日程 {schedule_id} 同时勾了会话组和具体会话，已按会话组保存（{labels}）"
            )
            cleaned.append({**item, "sessions": kept})
        if not changed:
            return schedules, []
        return {**schedules, "schedules": cleaned}, warnings

    def schedule_candidates(self, schedule: Any, session_id: str) -> list[str]:
        """这条日程能落在哪几个**具体会话**上（已过滤掉失效 / 停用 / 不在组里的）。

        勾了会话组：返回 []——组是「让她自己在组内挑一处」（见
        ``resolve_schedule_target``），不是一次发一片，所以没有"候选广播目标"一说。
        没配组 / 没勾：空 list 交给调用方回落到执行会话。
        """

        picked = self.schedule_sessions_of(schedule)
        group = self.group_of(session_id)
        if not picked or group is None:
            return []
        if str(group.id) in picked:
            return []
        members = {str(item) for item in (group.sessions or [])}
        return [item for item in picked if item in members and self.is_enabled(item)]

    def schedule_lock_targets(self, schedule: Any, session_id: str) -> list[str]:
        """日程这一轮的允许落点集合（写进 ``TickOutcome.locked_targets``）。

        - 勾了具体会话：锁 = 那几个启用中的具体会话（一个都没启用 = 不锁）；
        - 勾了会话组：锁 = 组里全部启用中的成员（她挑哪一处都在集合里）；
        - 什么都没勾：不锁（保持"每个会话各自跑"的老行为）。
        """

        picked = self.schedule_sessions_of(schedule)
        group = self.group_of(session_id)
        if not picked or group is None:
            return []
        members = [str(item) for item in (group.sessions or [])]
        if str(group.id) in picked:
            locked = [item for item in members if self.is_enabled(item)]
            return locked or [str(session_id)]
        candidates = self.schedule_candidates(schedule, session_id)
        return candidates or [str(session_id)]

    def schedule_targets(self, schedule: Any, session_id: str) -> list[str]:
        """这条日程这一轮要发到的**全部具体落点**（主目标 + 广播目标）。

        勾了会话组：返回空——组 = 由她在组内挑一处，不是一次发一片。
        勾了具体会话：返回所有启用中的那几处；一个可用的都没有就回落到执行会话。
        """

        picked = self.schedule_sessions_of(schedule)
        group = self.group_of(session_id)
        if not picked or group is None:
            return [str(session_id)]
        if str(group.id) in picked:
            return []
        return self.schedule_candidates(schedule, session_id) or [str(session_id)]

    def schedule_target(self, schedule: Any, session_id: str) -> str:
        """这条日程的**主落点**落在哪个会话里说。

        勾了具体会话：只在**启用中的**具体会话里挑（当前会话优先，其次列表第一个）；
        没有可用的具体会话就回落到执行会话——以前这里不查 ``is_enabled``，会挑到一个
        已经从白名单里删掉的会话，那句话就发进了一个不存在的群。
        勾了会话组 / 什么都没勾：按当前会话（组内的具体挑法见 ``resolve_schedule_target``）。
        """

        picked = self.schedule_sessions_of(schedule)
        if not picked:
            return str(session_id)
        group = self.group_of(session_id)
        if group is None:
            return str(session_id)
        if str(group.id) in picked:
            return str(session_id)
        candidates = self.schedule_candidates(schedule, session_id)
        if not candidates:
            return str(session_id)
        if str(session_id) in candidates:
            return str(session_id)
        return candidates[0]

    async def resolve_schedule_target(
        self, state: WorldState, schedule: Any, session_id: str
    ) -> str:
        """日程的落点：勾了会话组就**让大模型挑**组里的哪个会话。

        以前"勾组"等于落在组代表身上，用户看着就是"我说了落在组里，她却总是
        发到那一个群"。现在勾组 = 把这句话交给组里的某一个会话，由她自己挑
        （挑不动 / 没配模型时才回落到代表会话）。
        勾了具体会话的照旧，不问模型。
        """

        picked = self.schedule_sessions_of(schedule)
        group = self.group_of(session_id)
        if not picked or group is None:
            return self.schedule_target(schedule, session_id)
        group_picked = str(group.id) in picked
        pinned = self.schedule_candidates(schedule, session_id)
        if not group_picked or pinned:
            return self.schedule_target(schedule, session_id)
        members = [
            str(item)
            for item in (group.sessions or [])
            if self.is_enabled(str(item))
        ]
        if len(members) <= 1:
            return self.schedule_target(schedule, session_id)
        chosen = await self._pick_schedule_session(state, schedule, members)
        return chosen or self.schedule_target(schedule, session_id)

    @model_operation("schedule", "为日程选择合适的发送会话")
    async def _pick_schedule_session(
        self, state: WorldState, schedule: Any, members: list[str]
    ) -> str:
        """这句话说给组里的哪一处：**由她自己（主模型）看着上下文决定**。

        不走打杂模型：落点是她"开口说给谁听"的一部分，得带着人设和最近的
        聊天上下文判断——谁刚跟她说过话、哪一处正热闹。挑不出来返回空串
        （调用方回落到组代表）。
        """

        if self.llm is None:
            return ""
        labels = self.session_labels(state)
        window = self.chat_window(state)
        lines: list[str] = []
        for index, item in enumerate(members):
            label = labels.get(item) or self.session_label(item, state) or item
            recent = ""
            for row in reversed(window):
                origin = str(row.get("origin") or state.session_id)
                if origin != item:
                    continue
                text = " ".join(str(row.get("text") or "").split())
                if not text:
                    continue
                who = str(row.get("name") or row.get("user_id") or "")
                recent = f"{who}：「{_clip_text(text, 40)}」" if who else f"「{_clip_text(text, 40)}」"
                break
            lines.append(
                f"{index + 1}. {label}"
                + (f" —— 最近在这儿说：{recent}" if recent else " ——（最近没人在这儿说话）")
            )
        steps = "、".join(
            self.prompts.action_label(step.type)
            for step in (getattr(schedule, "action_chain", None) or [])
            if getattr(step, "type", "")
        )
        persona = await self._persona_text(state.session_id)
        system = (
            (persona + "\n\n" if persona else "")
            + "你正要按日程开口说一句话，得先决定这句话说给哪一处听。"
            "只输出一个数字（上面列表里的编号），不要解释、不要标点。"
        )
        prompt = (
            f"# 现在\n"
            f"你在「{self._node_name(state.node_id)}」，现在 {self.local_now().strftime('%H:%M')}。\n\n"
            f"# 日程到点了\n"
            f"要做的事：{steps or '（没写具体动作）'}\n"
            f"当初为什么排：{str(getattr(schedule, 'note', '') or '') or '（没写）'}\n\n"
            f"# 你能说话的地方\n" + "\n".join(lines) + "\n\n"
            "按你自己的判断挑一处：谁正等着你、哪一处更该听到这句话。只回编号。"
        )
        # 走主模型（带人设、带上下文），不是打杂模型
        reply = await self._ask_llm(state.session_id, system, prompt)
        if reply is None:
            return ""
        text = " ".join(str(reply).split())
        digits = "".join(char for char in text if char.isdigit())
        if not digits:
            return ""
        index = int(digits[:2]) - 1
        if 0 <= index < len(members):
            return members[index]
        return ""

    def schedule_in_scope(self, schedule: Any, session_id: str) -> bool:
        """这条日程属不属于"这个她"。

        勾了会话组就按组算；勾了具体会话就按组里的成员算；什么都没勾 = 每个她都要跑。
        """

        picked = self.schedule_sessions_of(schedule)
        if not picked:
            return True
        group = self.group_of(session_id)
        if group is None:
            return str(session_id) in picked
        if str(group.id) in picked:
            return True
        return bool({str(member) for member in (group.sessions or [])}.intersection(picked))

    def session_note(self, session_id: str) -> str:
        """这个会话的备注：有备注就用备注，不看群名。"""

        session = self.session_config(session_id)
        return " ".join(str(getattr(session, "note", "") or "").split())

    def session_group_name(self, session_id: str) -> str:
        """平台给的群名（拿不到就是空串）。"""

        info = (getattr(self, "_session_names", None) or {}).get(str(session_id)) or {}
        return " ".join(str(info.get("name") or "").split())

    async def refresh_session_names(self, session_ids: list[str] | None = None) -> None:
        """Read optional group metadata outside state locks; adapter owns caching."""
        fetch = getattr(self.messenger, 'fetch_group_names', None)
        if not callable(fetch):
            return
        sessions = [item.session_id for item in self.sessions.sessions if item.type == 'group']
        # Relationship records can outlive a removed session configuration.
        sessions = list(dict.fromkeys([*sessions, *(session_ids or [])]))
        try:
            names = await fetch(sessions)
        except Exception:
            return
        if isinstance(names, dict):
            for sid in sessions:
                name = names.get(sid)
                if isinstance(name, str):
                    self.note_session_name(sid, name)

    def note_session_name(self, session_id: str, name: str) -> None:
        """把平台给的群名记下来（只在拿到时调）。"""

        text = " ".join(str(name or "").split())[:40]
        if not text:
            return
        self._session_names = getattr(self, "_session_names", {}) or {}
        self._session_names[str(session_id)] = {"name": text}

    def session_display_name(
        self, session_id: str, state: WorldState | None = None
    ) -> str:
        """她在提示词里看到的会话名：备注 > 群名；私聊再回退到对方昵称。

        私聊通常没有群名，就用"最近在这儿跟她说话的那个人"当名字——
        这样通讯录里写的是「私聊 2692047521「主人」」而不是一串数字。
        """

        note = self.session_note(session_id)
        if note:
            return note
        group_name = self.session_group_name(session_id)
        if group_name:
            return group_name
        session = self.session_config(session_id)
        private = session.type == 'private' if session is not None else ':PrivateMessage:' in str(session_id)
        if private and state is not None:
            info = (getattr(state, "session_activity", None) or {}).get(
                str(session_id)
            ) or {}
            who = " ".join(str(info.get("user_name") or "").split())
            if who:
                return who[:12]
        return ""

    def session_label(self, session_id: str, state: WorldState | None = None) -> str:
        """通讯录里的一行标题：``群 123456「小米粥群」`` 这种。"""

        session = self.session_config(session_id)
        kind = "私聊" if session is not None and session.type == "private" else "群"
        number = str(session_id or "").split(":")[-1] or str(session_id or "")
        name = self.session_display_name(session_id, state)
        return f"{kind} {number}「{name}」" if name else f"{kind} {number}"

    def session_directory(self, state: WorldState) -> str:
        """她能说话的地方：每条带号码、名字、最近有没有人跟她说话。"""

        group = self.group_of(state.session_id)
        if group is None:
            # 只有一个地方就没什么可挑的：不往提示词里加这段
            return ""
        members = [str(item) for item in (group.sessions or [])] if group else [
            str(state.session_id)
        ]
        if not members:
            members = [str(state.session_id)]
        now = self._now()
        activity = dict(getattr(state, "session_activity", None) or {})
        lines: list[str] = []
        for session_id in members:
            if not self.is_enabled(session_id):
                continue
            info = activity.get(session_id) or {}
            at = float(info.get("at") or 0.0)
            who = " ".join(str(info.get("user_name") or "").split())[:12]
            if at <= 0:
                when = "还没有近期发言记录"
            else:
                minutes = max(0, int((now - at) // 60))
                when = (
                    f"{who or '有人'}最近在这里发言是 "
                    + ("刚刚" if minutes < 1 else f"{minutes} 分钟前" if minutes < 60 else f"{minutes // 60} 小时前")
                )
            lines.append(f"- {self.session_label(session_id, state)}：{when}")
        if not lines:
            return ""
        return (
            "# 你能说话的地方\n"
            + f"你现在在【{self.session_label(state.session_id, state)}】里说话。\n"
            + "\n".join(lines)
            + "\n这些都是你，只是说话的地方不同。除了回别人的话（在哪儿被问就在哪儿答），"
            "你想主动说什么就说给谁；下面的时间只是给你参考，怎么挑由你自己决定。"
            "**位置、心情、日程、记忆、你和每个人的关系只有一份**，换个地方说话不会变成另一个你。"
            "同一件事可以一次给两处各写一句（比如群里应付一句、私聊里再吐槽一句）——"
            "把两条 say 分别写上 send_to 就行。\n"
            "**上面这份名单就是你能说话的全部地方**：别人让你私聊他、加好友、"
            "或者去别的地方找他，而那人不在名单里，你就是过不去——那就别答应，"
            "用你自己的口气说明白（「本小姐就在这儿待着，要找我过来找」那种），"
            "别说「我私聊你」，也别把本来想私下说的话发在群里。"
        )

    def resolve_send_to(self, state: WorldState, value: Any, *, fallback: str) -> str:
        """把她写的落点（会话 id / 群号 / 昵称 / 备注）对上真正的会话。"""

        text = " ".join(str(value or "").split()).strip()
        if not text:
            return fallback
        group = self.group_of(state.session_id)
        members = [str(item) for item in (group.sessions or [])] if group else [
            str(state.session_id)
        ]
        for session_id in members:
            if not self.is_enabled(session_id):
                continue
            if self.session_ref_matches(session_id, text):
                return str(session_id)
        return fallback

    def session_ref_matches(self, session_id: str, text: str) -> bool:
        """她写的一串字能不能指到这个会话（会话 id / 号码 / 备注 / 群名）。"""

        lowered = " ".join(str(text or "").split()).lower()
        if not lowered:
            return False
        number = str(session_id).split(":")[-1]
        names = [self.session_note(session_id), self.session_group_name(session_id)]
        for candidate in [session_id, number, *names]:
            if candidate and str(candidate).lower() in lowered:
                return True
        return False

    def resolve_session_refs(
        self, values: Any, *, session_id: str = ""
    ) -> list[str]:
        """把一组"落点"说法换成会话 id / 会话组 id（日程用）。"""

        refs = [str(session_id)] if session_id else list(self.enabled_session_ids())
        picked: list[str] = []
        for raw_value in list(values or []):
            text = " ".join(str(raw_value or "").split())
            if not text:
                continue
            if self.group_by_id(text) is not None:
                if text not in picked:
                    picked.append(text)
                continue
            for ref in refs:
                group = self.group_of(ref)
                members = (
                    [str(item) for item in (group.sessions or [])]
                    if group
                    else [str(ref)]
                )
                hit = next(
                    (item for item in members if self.session_ref_matches(item, text)),
                    "",
                )
                if hit and hit not in picked:
                    picked.append(hit)
                if hit:
                    break
        return picked

    def default_node_id(self) -> str:
        return self.world.default_node_id()

    def node(self, node_id: str) -> NodeDef | None:
        return self.world.node_map().get(node_id)
