"""日程动作与「什么时候」的解析。"""

from __future__ import annotations

from ..model_calls import model_operation
from ..speech_policy import SILENT_FOLLOWUP

from datetime import (
    datetime,
    timedelta,
)
import re
from typing import Any
from ..config_scope import session_scoped
from ..json_actions import (
    MAX_SAY_LINES_HARD,
    PlannedAction,
    extract_json_object,
)
from ..models import (
    ActionDef,
    NodeDef,
)
from ..state import WorldState
from .models import TickOutcome
from .util import (
    _RELATIVE_WHEN,
    _clip_log,
    _clip_text,
    _duration_number,
    _spoken_hour,
)


class ScheduleActionsMixin:
    # ---------------- 日程：查看 / 添加 / 删除（内置动作与对外工具共用） ----------------

    async def _run_command_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        event: Any = None,
        *,
        out: dict[str, Any] | None = None,
        speak: bool = True,
        cached: dict[str, Any] | None = None,
        remember: dict[str, Any] | None = None,
    ) -> None:
        """指令触发：把意图拼成一条指令，交给 AstrBot 去执行，再把结果交回给她。

        有真实消息事件时直接用那条事件（图片、引用都跟着走）；
        自主触发时借用这个会话最近一条事件——所以在这种情况下没有图片。

        ``speak=False`` 只执行不说话，把结果写进 ``out``；``cached`` 反过来——
        指令早就跑过了，这里只负责把当初的结果交回给她。
        """

        command = str(definition.trigger_command or "").strip()
        if not command:
            outcome.notes.append(f"动作 {definition.id} 没有配置要触发的指令，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": "指令型动作没有配置要触发的指令"},
                outcome=outcome,
            )
            return
        intent = (action.intent or action.content or "").strip()
        if not intent:
            # 日程里的步骤没有"想干什么"这一栏：用动作自己的说明兜一句
            intent = self.fallback_intent(definition)

        if cached is None:
            line = await self._compose_command(state, definition, command, intent)
            if self.commands is None:
                outcome.notes.append("没有可用的指令通道，已跳过")
                return
            await self._log_event(
                state,
                "command_call",
                {"action": definition.id, "command": line, "intent": intent},
                outcome=self._echo_into(outcome, state),
            )
            # 指令在"她现在说话的地方"触发：私聊里让她拍照，指令就带着私聊的上下文跑
            # 落点当场定下来，图和正文都用它——不然私聊里起的动作会把图发到群里
            landing = self._image_home(outcome, action)
            call = await self.commands.trigger(landing, line)
            images = list(getattr(call, "image_urls", None) or [])
            # 指令生成出来的图（多张也算）贴着正文一起发，别只交回给她自己看
            attachments = list(getattr(call, "attachments", None) or [])
            self._attach_images(outcome, attachments, session_id=landing)
            if attachments:
                self._note_own_image(
                    state,
                    intent or definition.name or definition.id,
                    landing,
                )
            await self._log_event(
                state,
                "command_result",
                {
                    "action": definition.id,
                    "command": line,
                    "ok": bool(call.ok),
                    "result": _clip_log(call.text),
                    "images": len(images),
                    "error": call.error,
                },
                outcome=self._echo_into(outcome, state),
            )
            outcome.notes.append(
                f"触发指令「{line}」：" + ("成功" if call.ok else f"失败（{call.error}）")
            )
            if (
                definition.id == "check_weather"
                and call.ok
                and str(call.text or "").strip()
            ):
                # 查天气配成「指令型」时，结果同样要进全局天气记录
                await self._store_weather(
                    state,
                    definition,
                    {"tool_result": call.text, "tool_images": images},
                    source="manual",
                )
            payload = {
                "line": line,
                "ok": bool(call.ok),
                "text": str(call.text or ""),
                "images": images,
                "error": str(call.error or ""),
            }
            ok = payload["ok"]
            if remember is not None:
                # 让调用方拿到结果（状态槽、续说都要用）
                remember["tool_result"] = payload["text"]
                remember["tool_ok"] = payload["ok"]
                remember["tool_error"] = payload["error"]
                if images:
                    remember["tool_images"] = list(images)
            if out is not None:
                out.update(payload)
            if not speak:
                # 只执行不说话：结果先留着，等这一步真的做完再交回给她
                return
            detail = (
                payload["text"]
                if payload["ok"]
                else f"（这条指令没跑成：{payload['error']}）"
            )
        else:
            line = str(cached.get("line") or "")
            ok = bool(cached.get("ok"))
            images = list(cached.get("images") or [])
            detail = (
                str(cached.get("text") or "")
                if ok
                else f"（这条指令没跑成：{cached.get('error') or ''}）"
            )
        detail = self._clip_followup(detail)
        if not detail and images:
            detail = "这条指令返回了一张图片，图片一起发给你了。"

        # 说完不说一句，看动作里的「完成后」和全局的「工具结果回话」——
        # 与工具型动作同一套规则，不然用户想让她执行完指令别吭声也关不掉。
        trigger = str(definition.on_complete.trigger or "none")
        hint = str(definition.on_complete.prompt_hint or "").strip()
        want_followup = trigger == "llm_followup"
        if (
            not want_followup
            and trigger == "none"
            and bool(self.world.tool_result_reply)
            and (detail or images)
        ):
            want_followup = True
        if SILENT_FOLLOWUP.get() or (remember or {}).get("silent_followup"):
            want_followup = False
        if not want_followup:
            outcome.notes.append(f"指令「{line}」已执行，这次不开口")
            return
        if not hint:
            hint = (
                "用你自己的话把这条指令返回的内容讲一句，别照抄格式、别编。"
                if ok
                else "这条指令没跑成，用一句自然的话说明一下，别编内容。"
            )
        await self._llm_followup(
            state,
            node,
            outcome,
            hint,
            detail or "（没有返回内容）",
            image_urls=images,
        )

    @model_operation("schedule", "日程需要生成转发指令参数")
    async def _compose_command(
        self,
        state: WorldState,
        definition: ActionDef,
        command: str,
        intent: str,
    ) -> str:
        """把意图拼成一条完整指令；补不出参数就退回指令名本身。"""

        base = command if command.startswith("/") else f"/{command}"
        if self.helper_llm is None or not self._tool_param_allowed(state):
            return base
        system_prompt, prompt = self.prompts.build_command_prompt(
            command=base,
            hint=str(definition.trigger_hint or ""),
            intent=intent,
            # 出图类指令的参数就是一段画面描述：得让她知道此刻在哪儿、穿着什么，
            # 缺的那几项才能按现状补齐（而不是留空或者瞎编）
            state_text=self._compose_state_text(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        text = " ".join(str(reply or "").split()).strip().strip("`")
        if not text:
            return base
        # 补参模型偶尔会回一坨 JSON 或一整句话：那不是指令，宁可只发指令名本身，
        # 也别往群里发一条 `/{"actions":...}` 这种莫名其妙的东西。
        if any(mark in text for mark in ('{', '}', '[', ']', '"', "'")):
            self._log(
                "debug",
                f"指令参数补全返回的内容不像指令，已退回指令名：{_clip_text(text, 80)}",
            )
            return base
        return text if text.startswith("/") else f"/{text}"

    def _compose_state_text(self, state: WorldState) -> str:
        """拼指令参数时给她「此刻是什么样」：地点、手头的事、心情、状态槽。

        出图 / 出视频这类指令的参数就是一段画面描述，光有意图写不出完整画面
        （服装、环境、光线都在她当前的状态里）。这里只给事实，不替她编。
        """

        lines: list[str] = []
        node = self.node(state.node_id) or self.node(self.default_node_id())
        if node is not None:
            scene = " ".join(str(getattr(node, "prompt", "") or "").split())
            line = f"- 地点：{node.name or node.id}"
            if scene:
                line += f"——{_clip_text(scene, 90)}"
            lines.append(line)
        current = state.current_action if isinstance(state.current_action, dict) else None
        current_type = str((current or {}).get("type") or "")
        if current_type:
            lines.append(f"- 手头的事：{self.prompts.action_label(current_type)}")
        if str(state.mood or "").strip():
            lines.append(f"- 心情：{state.mood}")
        now = self._now()
        for slot, info in dict(getattr(state, "external_state", None) or {}).items():
            if not isinstance(info, dict):
                continue
            text = " ".join(str(info.get("text") or "").split())
            if not text:
                continue
            try:
                expires = float(info.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires = 0.0
            if expires and now > expires:
                continue
            label = str(info.get("label") or slot).strip()
            lines.append(f"- {label}：{_clip_text(text, 140)}")
        return "\n".join(lines)

    SCHEDULE_ACTION_IDS = ("schedule_list", "schedule_add", "schedule_remove")

    def schedule_text(self) -> str:
        """把所有日程渲染成一行行给模型看的文本。"""

        items = list(getattr(self.schedules, "schedules", None) or [])
        if not items:
            return "（现在没有任何日程）"
        lines = []
        for item in items:
            chain = " → ".join(step.type for step in (item.action_chain or []) if step.type)
            days = "/".join(item.days or [])
            who = "她自己加的" if str(getattr(item, "created_by", "") or "") == "bot" else "用户配的"
            state = "启用" if item.enabled else "停用"
            when = f"{item.date} " if (item.once and item.date) else ""
            line = (
                f"- {item.id}｜{when}{item.time}｜{days}｜{chain or '（空）'}｜{state}｜{who}"
                + ("｜只做这一次" if item.once else "")
                + ("｜到点自动先走过去" if item.auto_travel else "")
            )
            landed = [
                str(value) for value in (getattr(item, "sessions", None) or []) if str(value)
            ]
            if landed:
                line += "｜落点：" + "、".join(
                    self.session_label(value) if ":" in value else f"会话组 {value}"
                    for value in landed
                )
            else:
                line += "｜落点：每个会话各自跑"
            note = " ".join(str(getattr(item, "note", "") or "").split())
            if note:
                line += f"｜当初为什么排：{note}"
            lines.append(line)
        return "\n".join(lines)

    @session_scoped
    async def schedule_add(
        self, payload: dict[str, Any], *, session_id: str = ""
    ) -> tuple[bool, str]:
        """加一条她自己安排的日程（校验时间、星期、动作链）。"""

        raw = self.scoped_schedules()
        items = list(raw.get("schedules") or [])
        time_text = " ".join(str(payload.get("time") or "").split())
        # 常见写法都收："7:05" / "07:5" / "7:5" 统一成 HH:MM
        if not re.match(r"^\d{1,2}:\d{1,2}$", time_text):
            return False, f"时间「{time_text or '（空）'}」看不懂，要写成 HH:MM"
        hour, minute = (int(part) for part in time_text.split(":"))
        if hour > 23 or minute > 59:
            return False, f"时间「{time_text}」不对"
        time_text = f"{hour:02d}:{minute:02d}"

        days = [
            str(day)
            for day in (payload.get("days") or [])
            if str(day) in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        ]
        if not days:
            days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        chain: list[dict[str, Any]] = []
        for step in payload.get("action_chain") or []:
            if not isinstance(step, dict):
                continue
            action_id = str(step.get("type") or step.get("action") or "").strip()
            definition = self.world.action_map().get(action_id)
            if definition is None or not definition.enabled:
                return False, f"动作「{action_id or '（空）'}」不存在或已停用，这条日程没加成"
            if action_id in self.SCHEDULE_ACTION_IDS:
                # 日程里再去排日程是个死循环，只会到点报错
                return False, "日程里不能再排日程，换个到点要做的动作"
            item: dict[str, Any] = {"type": action_id}
            for key in ("target_node", "target", "content", "duration", "messages", "params"):
                if step.get(key):
                    item[key] = step[key]
            chain.append(item)
        if not chain:
            return False, "日程里至少要有一个动作"
        if len(chain) > max(1, int(self.world.limits.max_action_chain_depth) + 2):
            return False, "日程里的动作太多了，拆成两条吧"

        schedule_id = str(payload.get("id") or "").strip()
        if not schedule_id:
            schedule_id = f"bot_{int(self._now())}"
        if any(str(item.get("id")) == schedule_id for item in items):
            return False, f"已经有一条叫「{schedule_id}」的日程了"

        # 一次性日程：只在那一天跑一遍，跑完就删。日期写不出来就退化成「每天」，
        # 免得模型给个看不懂的日期之后这条日程永远不会到点。
        once = bool(payload.get("once"))
        date_text = str(payload.get("date") or "").strip()
        if once and date_text:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
                once, date_text = False, ""
            else:
                try:
                    datetime.strptime(date_text, "%Y-%m-%d")
                except ValueError:
                    once, date_text = False, ""
        note = " ".join(str(payload.get("note") or "").split())[:120]
        # 落点：她可能写的是会话名 / 群号 / 备注，也可能直接写会话组 id
        landed = self.resolve_session_refs(payload.get("sessions"), session_id=session_id)

        items.append(
            {
                "id": schedule_id,
                "enabled": True,
                "time": time_text,
                "days": days,
                "action_chain": chain,
                "once": once,
                "date": date_text if once else "",
                "note": note,
                "auto_travel": bool(payload.get("auto_travel", True)),
                "conditions": dict(payload.get("conditions") or {}),
                "sessions": landed,
                "priority": 5,
                "created_by": "bot",
            }
        )
        self.save_scoped_schedules(
            {**raw, "schedules": items}, reason="她给自己加了日程"
        )
        self.reload_config()
        names = " → ".join(
            (self.world.action_map().get(step["type"]).name or step["type"])
            if self.world.action_map().get(step["type"])
            else step["type"]
            for step in chain
        )
        when = f"{date_text} {time_text}（只做这一次）" if once else f"{time_text} 的日程"
        return True, f"{when}加好了：{names}"

    def _drop_schedule(self, schedule_id: str) -> bool:
        """按 id 摘掉一条日程（一次性日程跑完就用这个收尾）。"""

        raw = self.scoped_schedules()
        items = list(raw.get("schedules") or [])
        kept = [item for item in items if str(item.get("id")) != str(schedule_id)]
        if len(kept) == len(items):
            return False
        self.save_scoped_schedules({**raw, "schedules": kept}, reason="日程跑完了回收")
        self.reload_config()
        return True

    def _prune_once_schedules(self, day_key: str) -> None:
        """日期已经过去的一次性日程留着没用，顺手清掉（每天只扫一次）。"""

        owner = self.config_scope_id if "schedules" in (getattr(self.session_config(self.config_scope_id), "config_overrides", {}) or {}) else ""
        if day_key == self._once_pruned_day.get(owner):
            return
        self._once_pruned_day[owner] = day_key
        raw = self.scoped_schedules()
        items = list(raw.get("schedules") or [])
        kept = [
            item
            for item in items
            if not (
                item.get("once")
                and str(item.get("date") or "")
                and str(item.get("date")) < day_key
            )
        ]
        if len(kept) == len(items):
            return
        self.save_scoped_schedules(
            {**raw, "schedules": kept}, reason="清理过期的一次性日程"
        )
        self.reload_config()

    async def schedule_remove(self, selector: dict[str, Any]) -> tuple[bool, str]:
        """删掉她自己加的一条日程（用户配的动不了）。"""

        raw = self.scoped_schedules()
        items = list(raw.get("schedules") or [])
        target_id = str(selector.get("id") or "").strip()
        time_text = str(selector.get("time") or "").strip()
        keyword = str(selector.get("keyword") or "").strip()
        matched = None
        for item in items:
            if target_id and str(item.get("id")) == target_id:
                matched = item
                break
            if time_text and str(item.get("time")) == time_text:
                matched = item
                break
            if keyword:
                blob = " ".join(
                    [str(item.get("id") or ""), str(item.get("time") or "")]
                    + [str(step.get("type") or "") for step in item.get("action_chain") or []]
                )
                if keyword in blob:
                    matched = item
                    break
        if matched is None:
            return False, "没找到那条日程"
        if str(matched.get("created_by") or "") != "bot":
            return False, "那条是用户自己配的日程，我删不了"
        items = [item for item in items if item is not matched]
        self.save_scoped_schedules(
            {**raw, "schedules": items}, reason="她自己删了一条日程"
        )
        self.reload_config()
        return True, f"删掉了 {matched.get('time')} 那条日程"

    async def _run_schedule_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
    ) -> None:
        """日程三件套。

        「提醒类」日程直接在代码里拼好（模型只要给时间 + 到时候说什么），
        复杂的（到点做一串动作）才交给辅助模型解析——那条路会改写她的原话，
        一条单纯的提醒没必要绕一圈。
        """

        op = {
            "schedule_list": "list",
            "schedule_add": "add",
            "schedule_remove": "remove",
        }.get(definition.id, "list")
        intent = (action.intent or action.content or "").strip()
        payload: dict[str, Any] = {}
        if op in ("add", "remove"):
            if not intent:
                await self._log_event(
                    state,
                    "schedule_edit",
                    {"op": op, "ok": False, "note": "没有说清要做什么"},
                    outcome=self._echo_into(outcome, state),
                )
                outcome.notes.append("日程动作没有给出意图，已跳过")
                return
            payload = None
            if op == "add":
                payload = self._schedule_from_action(action, intent)
            if payload is None:
                payload = await self._parse_schedule_intent(state, op, intent)

        if op == "list":
            ok, note = True, self.schedule_text()
        elif op == "add":
            ok, note = await self.schedule_add(payload, session_id=state.session_id)
        else:
            ok, note = await self.schedule_remove(payload)

        await self._log_event(
            state,
            "schedule_edit",
            {"op": op, "ok": bool(ok), "note": note, "raw": payload},
            outcome=self._echo_into(outcome, state),
        )
        outcome.notes.append(f"日程 {op}：{note}")
        hint = (
            "用一句自然的话说说你刚安排的这件事（或者刚刚看到的日程），别念清单。"
            if ok
            else "刚才那件事没办成，用一句自然的话说明一下，别编。"
        )
        await self._llm_followup(state, node, outcome, hint, note)

    @staticmethod
    def _param_lines(value: Any) -> list[str]:
        """把模型给的台词参数（字符串 / 列表）整理成一行行。"""

        if isinstance(value, str):
            items: list[Any] = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            return []
        lines: list[str] = []
        for item in items:
            text = " ".join(str(item or "").split())
            if text:
                lines.append(text)
        return lines[:MAX_SAY_LINES_HARD]

    def _when_payload(
        self, target: datetime, base: datetime, *, recurring: bool
    ) -> dict[str, Any]:
        """把"什么时候"整理成日程字段：一次性写 once + date，循环的只写 time。"""

        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        once = not recurring
        return {
            "time": target.strftime("%H:%M"),
            "days": list(days),
            "once": once,
            "date": target.strftime("%Y-%m-%d") if once else "",
        }

    def _parse_when_text(self, text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """从一句话里读「什么时候」：相对时间（十分钟后）或绝对时刻（21:30 / 晚上八点）。

        读不出来返回 ``None``，调用方会回落到辅助模型。
        """

        body = " ".join(str(text or "").split())
        if not body:
            return None
        base = now if isinstance(now, datetime) else self.local_now()
        recurring = any(
            word in body for word in ("每天", "天天", "每晚", "每早", "每周", "每周一")
        )
        relative = _RELATIVE_WHEN.search(body)
        if relative:
            amount = _duration_number(relative.group("num"))
            if amount:
                unit = relative.group("unit") or ""
                minutes = int(round(amount * (60.0 if unit in ("小时", "钟头") else 1.0)))
                if minutes > 0:
                    return self._when_payload(
                        base + timedelta(minutes=minutes), base, recurring=recurring
                    )
        absolute = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", body)
        if absolute:
            hour, minute = int(absolute.group(1)), int(absolute.group(2))
            if hour <= 23 and minute <= 59:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if "明天" in body or target <= base:
                    target += timedelta(days=1)
                return self._when_payload(target, base, recurring=recurring)
        spoken = _spoken_hour(body)
        if spoken is not None:
            hour, minute = spoken
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if "明天" in body or target <= base:
                target += timedelta(days=1)
            return self._when_payload(target, base, recurring=recurring)
        return None

    def _schedule_from_action(
        self, action: PlannedAction, intent: str
    ) -> dict[str, Any] | None:
        """「提醒类」日程：时间 + 到时候说的话都在模型给的参数里，直接拼，不用辅助模型。

        少一次模型调用，也不会把她的原话改写走样（"提醒她喝水"被写成
        "提醒自己不要喝水"这种事就是这么来的）。拼不出来返回 None。
        """

        params = dict(action.params or {})
        when = self._parse_when_text(
            str(params.get("at") or params.get("when") or params.get("time") or "")
            or intent
        )
        if not when:
            return None
        lines = self._param_lines(
            params.get("say")
            or params.get("messages")
            or params.get("content")
            or params.get("text")
        )
        if not lines:
            return None
        payload: dict[str, Any] = {
            **when,
            "action_chain": [{"type": "say", "messages": lines}],
            "note": intent,
            "auto_travel": False,
        }
        sessions = params.get("sessions")
        if sessions:
            payload["sessions"] = (
                sessions if isinstance(sessions, list) else [sessions]
            )
        return payload

    @model_operation("schedule", "解析日程里的自然语言意图")
    async def _parse_schedule_intent(
        self, state: WorldState, op: str, intent: str
    ) -> dict[str, Any]:
        """让辅助模型把「每天七点查新闻」翻成结构化日程参数。"""

        if self.helper_llm is None or not self._tool_param_allowed(state):
            return {"raw": intent}
        action_lines = [
            f"- {item.id}：{item.name or item.id}"
            for item in self.world.actions
            if item.enabled
        ]
        system_prompt, prompt = self.prompts.build_schedule_action_prompt(
            op=op,
            intent=intent,
            actions=action_lines,
            current=self.schedule_text(),
            now=self.local_now(),
            sessions=self.session_directory(state),
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        return payload if isinstance(payload, dict) else {"raw": intent}
