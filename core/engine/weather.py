"""天气：拉取、缓存与提示词那一行。"""

from __future__ import annotations

from typing import Any
from ..config_scope import session_scoped
from ..defaults import DEFAULT_WEATHER_PROMPT
from ..json_actions import (
    PlannedAction,
    parse_action_payload,
)
from ..memory import (
    INNER,
    SCENE,
)
from ..models import (
    ActionDef,
    NodeDef,
)
from ..mood import day_mood_info
from ..planner import (
    active_plan,
    advance,
    peek_step,
)
from ..speech_policy import speech_origin
from ..state import (
    STATE_DROWSY,
    STATE_IDLE,
    WorldState,
)
from ..weather import (
    WEATHER_KEY,
    WEATHER_TRY_KEY,
    WeatherRecord,
    city_from_tool_args,
    looks_like_weather_tool,
    parse_parts,
    banner as weather_banner,
    prompt_block as weather_prompt_block,
)
from .constants import (
    SEARCH_LOG_KEY,
    SEARCH_LOG_MINUTES,
)
from .models import TickOutcome
from .util import (
    _clip_text,
    normalize_param_schema,
)


class WeatherMixin:
    # ================= 天气 =================

    async def weather_record(self) -> WeatherRecord:
        """当前那份天气（全局共享，重启后仍在）。"""

        try:
            payload = await self.db.call("kv_get", WEATHER_KEY)
        except Exception:
            return WeatherRecord()
        return WeatherRecord.from_payload(payload)

    async def weather_line(self) -> str:
        """写进提示词的那一段；关掉、没记录、或者旧到不值得提都返回空串。"""

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)):
            return ""
        record = await self.weather_record()
        if record.empty:
            return ""
        return weather_prompt_block(
            record,
            self._now(),
            stale_hours=float(getattr(config, "stale_hours", 24.0) or 0.0),
            tz=self._resolve_tz(),
        )

    async def weather_payload(self) -> dict[str, Any]:
        """编辑器横幅要的数据。"""

        record = await self.weather_record()
        return weather_banner(record, self._now(), tz=self._resolve_tz())

    async def runtime_notes(self, session_id: str) -> dict[str, str]:
        """提示词里那几段"此刻的事实"：天气 + 最近查过什么。"""

        notes = {
            "weather": await self.weather_line(),
            "recent_search": await self._recent_search_line(session_id),
        }
        if not session_id:
            return notes
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return notes
        # 能力值给的是人话；心里挂着的事按需注入（没挂着就整段不出现）
        notes["abilities_line"] = self.abilities_line(state)
        notes["pending_event"] = self.pending_event_line(state)
        # 她自己的账：正在经历 / 最近发生在我身上的事 / 还挂着的（窗口外的不出现）
        notes["event_journal"] = self.event_journal(state)
        return notes

    async def _recent_search_line(self, session_id: str) -> str:
        """最近一次检索的摘要（一小时内才写进提示词）：省得她拿同样的词再查一遍。"""

        if not session_id:
            return ""
        try:
            payload = await self.db.call("kv_get", f"{SEARCH_LOG_KEY}.{session_id}")
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        try:
            at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            return ""
        age = self._now() - at
        if at <= 0 or age < 0 or age > SEARCH_LOG_MINUTES * 60:
            return ""
        queries = [str(item) for item in (payload.get("queries") or []) if str(item)][:3]
        if not queries:
            return ""
        minutes = max(1, int(round(age / 60)))
        found = int(payload.get("found") or 0)
        return (
            "# 最近查过\n"
            f"{minutes} 分钟前你查过「{'」「'.join(queries)}」，拿到 {found} 条材料。"
            "同一件事不要马上再查一遍；要接着查就换个更具体的角度。\n"
            "如果这批材料你还没讲给群里听过，就直接讲内容——不要再回一句"
            "「正在挑 / 马上端上来 / 等着」这类话。"
        )

    async def _remember_search(
        self, session_id: str, queries: list[str], found: int
    ) -> None:
        if not session_id or not queries:
            return
        try:
            await self.db.call(
                "kv_set",
                f"{SEARCH_LOG_KEY}.{session_id}",
                {
                    "at": float(self._now()),
                    "queries": [str(item) for item in queries[:5]],
                    "found": int(found),
                },
            )
        except Exception:
            pass

    async def weather_next_at(self) -> float:
        """下一次静默刷新大约在什么时候（0 = 不会再刷新）。"""

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)):
            return 0.0
        hours = max(0.0, float(getattr(config, "refresh_hours", 2.0) or 0.0))
        if hours <= 0:
            return 0.0
        try:
            last = float(await self.db.call("kv_get", WEATHER_TRY_KEY) or 0.0)
        except Exception:
            last = 0.0
        if last <= 0:
            return self._now()
        return last + hours * 3600.0

    def _weather_channel(self, definition: ActionDef | None) -> str:
        """「查天气」这次走哪条路：``command`` 指令型 / ``tool`` 工具型 / ``""`` 两者都不是。"""

        if definition is None:
            return ""
        level = str(getattr(definition, "llm_level", "") or "")
        if level == "command":
            return "command"
        return "tool" if level == "tool" else ""

    async def maybe_refresh_weather(
        self, *, force: bool = False, session_id: str = ""
    ) -> str:
        """到点了就静默查一次天气：不说话、不发群，只更新那份记录。

        ``force=True`` 用于编辑器上的「立即刷新」：跳过倒计时，其余条件照旧。
        ``session_id`` 是编辑器里选中的那条会话：指令型动作要借它的消息当上下文。
        返回一句说明（编辑器直接弹给用户看，免得"点了没反应"）；空串表示成功。
        """

        config = self.world.weather
        if not bool(getattr(config, "enabled", True)) and not force:
            return "天气功能在全局设置里关着（地图上的「刷新」仍然可以手动查一次）"
        hours = max(0.0, float(getattr(config, "refresh_hours", 2.0) or 0.0))
        definition = self.world.action_map().get("check_weather")
        channel = self._weather_channel(definition)
        if not channel:
            # 不用再配「工具型动作」了：默认走工具那条路，
            # 具体用哪把天气工具由 _weather_once 自己从已注册的工具里认。
            channel = "tool"
        if channel == "command":
            if self.commands is None:
                return "拿不到 AstrBot 的指令通道"
            if not str(getattr(definition, "trigger_command", "") or "").strip():
                return "「查天气」是指令型，但没填要触发的指令名"
        elif self.tools is None:
            return "拿不到 AstrBot 的工具通道"
        if hours <= 0 and not force:
            # 间隔 0 = 不在后台查；手动点「刷新」照样要查
            return "后台刷新间隔是 0（只在手动点的时候查）"
        now = self._now()
        try:
            last_try = float(await self.db.call("kv_get", WEATHER_TRY_KEY) or 0.0)
        except Exception:
            last_try = 0.0
        if not force and last_try > 0 and (now - last_try) < hours * 3600:
            return "还没到下一次刷新的时间"
        if channel == "tool":
            # 工具需要一条真实消息当上下文：群里还没人说过话就先不查，等下一轮
            has_context = getattr(self.tools, "has_context", None)
            if callable(has_context) and not bool(has_context()):
                return "群里还没人说过话，工具拿不到消息当上下文——先让群里说一句再试"
        sessions = self.enabled_session_ids()
        wanted = str(session_id or "").strip()
        target = wanted if wanted and wanted in sessions else ""
        if not target and channel == "command":
            # 指令要借一条真实消息当上下文：优先白名单里最近说过话的那个会话
            picker = getattr(self.commands, "session_with_context", None)
            if callable(picker):
                try:
                    target = str(picker(sessions) or "")
                except Exception:
                    target = ""
        target = target or (sessions[0] if sessions else "")
        if not target:
            return "没有启用的会话（先在会话白名单里加一个群）"
        # 先记"试过了"：失败也要等下一个周期，不要每个 tick 都去撞
        try:
            await self.db.call("kv_set", WEATHER_TRY_KEY, float(now))
        except Exception:
            pass
        record, note = await self._weather_once(target)
        if not record.empty:
            return ""
        return note or "这次没查到（日志里有原因）"

    async def refresh_weather(self, session_id: str) -> WeatherRecord:
        """查一次天气并记下来（静默：不触发续说，也不回显到群里）。"""

        record, _note = await self._weather_once(session_id)
        return record

    async def _weather_once(self, session_id: str) -> tuple[WeatherRecord, str]:
        """查一次天气：返回 ``(记录, 没查到的原因)``；查到了就写进全局天气记录。"""

        definition = self.world.action_map().get("check_weather")
        channel = self._weather_channel(definition)
        if definition is None:
            # 动作被删了也能查：天气早就不是"必须有一个工具型动作"才能用的东西了，
            # 用什么工具由插件自己认（见 _weather_definition_with_tool）。
            definition = ActionDef(id="check_weather", name="查天气")
            channel = "tool"
        elif not channel:
            # 普通虚拟动作也走工具那条路：用户不需要再去动作库里把它改成工具型
            channel = "tool"
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return WeatherRecord(), "读不到她在那个会话里的状态，先让她在群里说一句话"
        if channel == "command":
            # 查天气配成「指令型」时就走指令通道
            command = str(getattr(definition, "trigger_command", "") or "").strip()
            if not command:
                return WeatherRecord(), "「查天气」是指令型，但没填要触发的指令名"
            if self.commands is None:
                return WeatherRecord(), "拿不到 AstrBot 的指令通道"
            city = str(getattr(self.world.weather, "city", "") or "").strip()
            intent = f"查一下{city}现在的天气" if city else "现在外面的天气"
            line = await self._compose_command(state, definition, command, intent)
            call = await self.commands.trigger(session_id, line)
            if not call.ok:
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": definition.id,
                        "note": f"查天气的指令「{line}」没跑成：{call.error or '没有返回内容'}",
                    },
                )
                return WeatherRecord(), (
                    f"指令「{line}」没跑成：{call.error or '没有返回内容'}"
                )
            if not str(call.text or "").strip() and not list(
                getattr(call, "image_urls", None) or []
            ):
                return WeatherRecord(), f"指令「{line}」跑完了，但没返回天气内容"
            record = await self._store_weather(
                state,
                definition,
                {
                    "tool_result": call.text,
                    "tool_images": list(getattr(call, "image_urls", None) or []),
                },
                source="auto",
            )
            if record.empty:
                return record, "指令返回了内容，但没能整理成天气（日志里有原因）"
            return record, ""
        # 没绑工具（或者绑的那个已经卸了）时，从已注册的工具里自动认一把天气工具：
        # 用户装了 uapipro 工具箱这类插件就能直接用上，不用回动作库手配。
        definition = self._weather_definition_with_tool(definition, state)
        if self.tools is None:
            return WeatherRecord(), "拿不到 AstrBot 的工具通道"
        if not self.resolve_tools(definition, state.node_id):
            return WeatherRecord(), (
                "「查天气」还没选工具，AstrBot 里也没找到像天气的工具："
                "先装一个天气工具（例如 uapipro 工具箱），或者把它改成指令型动作"
            )
        outcome = TickOutcome(session_id=session_id)
        outcome.place = session_id
        action = PlannedAction(type=definition.id, intent="现在外面的天气")
        city = str(getattr(self.world.weather, "city", "") or "").strip()
        if city:
            action.params = self._city_params(definition, state, city)
        if not await self._prepare_tool_action(state, definition, action, outcome):
            return WeatherRecord(), (
                "「查天气」选的天气工具在 AstrBot 里没注册或没选："
                "先确认工具装好了，或者把它改成指令型动作"
            )
        payload: dict[str, Any] = {
            "type": definition.id,
            "params": dict(action.params),
            "tool_params": dict(getattr(action, "tool_params", {}) or {}),
            "intent": action.intent,
            "queries": [],
        }
        # 这里刻意不传 outcome：静默刷新不该出现在群里（调试回显也不该）
        await self._run_tool_calls(state, definition, payload)
        record = await self._store_weather(state, definition, payload, source="auto")
        if record.empty:
            return record, "天气工具没返回可用的内容（日志里有原因）"
        return record, ""

    def weather_tool_candidates(self) -> list[str]:
        """已注册的工具里，哪些像是查天气的（按名字和说明认，不写死工具名）。"""

        found: list[str] = []
        for name, description in (self.available_tools() or {}).items():
            if looks_like_weather_tool(name, description):
                found.append(str(name))
        return found

    def _weather_definition_with_tool(
        self, definition: ActionDef | None, state: WorldState
    ) -> ActionDef | None:
        """「查天气」没绑工具时，自动挑一把已注册的天气工具。

        只在原本没绑、或者绑的那个已经不在时会挑；挑不到就原样返回，
        让上层照旧去提示用户自己绑。
        """

        if definition is None:
            return None
        if self.resolve_tools(definition, state.node_id):
            return definition
        candidates = self.weather_tool_candidates()
        if not candidates:
            return definition
        return definition.model_copy(
            update={"tool_names": [candidates[0]], "tool_fallbacks": []}
        )

    async def note_tool_weather(
        self,
        *,
        session_id: str,
        tool_name: str,
        args: Any = None,
        result: str = "",
        images: list[str] | None = None,
    ) -> bool:
        """原生工具查到天气时顺手记一份。

        主回复的工具调用已经在 AstrBot 原生那边跑完了，本插件只做记账：
        认得出是天气工具、又拿到了结果，就存进全局天气记录。这样地图横幅和
        下一轮的提示词立刻有真数据，不用再让本插件自己单独查一次。
        返回是否真的记下来了。
        """

        config = self.world.weather
        # 这里**不看**「启用天气」总开关：那个开关管的是"本插件要不要自己去查、
        # 要不要写进提示词"。主回复已经用原生工具查过一次了，记下来不额外花钱，
        # 地图横幅上能看见她刚查到的天气；不想记就把「记住她在聊天里查到的天气」关掉。
        if not bool(getattr(config, "from_tools", True)):
            return False
        if not session_id:
            return False
        description = str((self.available_tools() or {}).get(tool_name) or "")
        if not looks_like_weather_tool(tool_name, description):
            return False
        text = str(result or "").strip()
        pics = [str(item) for item in (images or []) if str(item).strip()]
        if not text and not pics:
            return False
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return False
        city = city_from_tool_args(args)
        if city and city not in text:
            # 把城市写在最前面：归一化的小模型就知道这轮问的是哪儿
            text = f"城市：{city}\n{text}"
        record = await self._store_weather(
            state,
            None,
            {"tool_result": text, "tool_images": pics},
            source="tool",
        )
        return not record.empty

    async def _store_weather(
        self,
        state: WorldState,
        definition: ActionDef | None,
        action: dict[str, Any],
        *,
        source: str,
    ) -> WeatherRecord:
        """把这次查到的天气写进全局记录；什么都没拿到就什么都不改。"""

        raw = str(action.get("tool_result") or "").strip()
        images = [str(item) for item in (action.get("tool_images") or []) if str(item)]
        if not raw and not images:
            return WeatherRecord()
        text = await self._normalize_weather(state, raw, images)
        if not text:
            return WeatherRecord()
        record = WeatherRecord(
            text=text,
            parts=parse_parts(text),
            at=self._now(),
            source=source,
            raw=_clip_text(raw, 2000),
            images=images,
        )
        try:
            await self.db.call("kv_set", WEATHER_KEY, record.to_payload())
        except Exception as exc:
            self._log("warning", f"天气记录写不进去：{exc}")
            return WeatherRecord()
        # 静默刷新也算"刚查过"：下一次静默刷新从头计时
        try:
            await self.db.call("kv_set", WEATHER_TRY_KEY, float(self._now()))
        except Exception:
            pass
        await self._log_event(
            state,
            "weather",
            {
                "action": str(getattr(definition, "id", "") or "native_tool"),
                "source": source,
                "text": record.line(),
                "images": len(images),
                "raw": _clip_text(raw, 300),
            },
        )
        return record

    async def _normalize_weather(
        self, state: WorldState, raw: str, images: list[str]
    ) -> str:
        """把天气结果压成「城市｜温度｜天气｜湿度｜风力｜预报」。

        返回图片先交给多模态模型读出来，再把文字交给小模型归一化；
        哪一步没配、或者读不出来，就退回原文，绝不编。
        """

        text = str(raw or "").strip()
        if images and self.describer is not None:
            described = ""
            try:
                described = await self.describer.describe_to_text(
                    images, DEFAULT_WEATHER_PROMPT
                )
            except Exception as exc:
                self._log("debug", f"天气图读不出来：{exc}")
            if described:
                text = f"{described}\n{text}".strip()
        if not text:
            return ""
        if parse_parts(text):
            return " ".join(text.split())[:200]
        if not bool(getattr(self.world.weather, "normalize", True)):
            return text
        if self.helper_llm is None or not self._tool_param_allowed(state):
            return text
        reply = await self._ask_helper(
            state.session_id,
            DEFAULT_WEATHER_PROMPT,
            f"天气内容：\n{_clip_text(text, 1200)}",
        )
        self._count_tool_param(state)
        cleaned = " ".join(str(reply or "").split())
        if not cleaned or "读不出" in cleaned:
            return "" if images else text
        # 模型没按格式给（拆不出字段）时保留原文：宁可她讲得啰嗦，也别丢信息
        return cleaned[:200] if parse_parts(cleaned) else text

    def _city_params(
        self, definition: ActionDef, state: WorldState, city: str
    ) -> dict[str, Any]:
        """把「全局设置里的城市」填进天气工具的城市参数（认得出参数名才填）。"""

        names = self.resolve_tools(definition, state.node_id)
        if not names:
            return {}
        key = self._city_param_name(names[0])
        return {key: city} if key else {}

    def _city_param_name(self, tool: str) -> str:
        """天气工具里"城市"该填哪个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        exact = {
            "city",
            "location",
            "place",
            "region",
            "area",
            "district",
            "城市",
            "地点",
        }
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in exact or "city" in low or "location" in low:
                    return name
        return required[0] if required else ""

    async def tick(self) -> list[TickOutcome]:
        """推进所有启用会话的世界时钟。返回需要发送的消息。"""

        self._last_tick_at = self._now()
        try:
            # 天气是全局的：整轮只查一次，放在会话循环之前，
            # 这样它写的事件也已经越过各会话的回显游标（静默刷新不快发到群里）
            await self.maybe_refresh_weather()
        except Exception as exc:
            self._log("debug", f"静默刷新天气失败：{exc}")
        try:
            # 自动排日程也是"每天一次"的事：放在会话循环之前，
            # 排好的安排这一轮就能按正常逻辑被看见。
            await self.maybe_auto_generate_schedules()
        except Exception as exc:
            self._log("debug", f"自动排日程失败：{exc}")
        try:
            # 心迹画像一直在记新东西：每隔几小时同步一份过来（只读它的文件）
            await self.maybe_sync_profile_weaver(
                str(getattr(self, "external_data_root", "") or "")
            )
        except Exception as exc:
            self._log("debug", f"心迹画像同步失败：{exc}")
        outcomes: list[TickOutcome] = []
        # 同一个她只推一次：组里的几个群 / 私聊共用一份状态，
        # 按会话各推一遍等于她的时间、精力、事件频率乘以会话数。
        for session_id in self.tick_session_ids():
            try:
                outcome = await self._tick_session(session_id)
            except Exception as exc:  # 单个会话出错不影响其他会话
                self._log("warning", f"tick 失败 session={session_id}: {exc}")
                continue
            if outcome is not None:
                outcomes.append(outcome)
        try:
            outcomes.extend(await self.run_schedules())
        except Exception as exc:
            self._log("warning", f"日程检查失败: {exc}")
        # 对话记忆：把攒着的片段总结成一条（放在这里是为了不占着会话锁调模型）
        for session_id in self.tick_session_ids():
            try:
                flushed = await self.flush_pending_memory(session_id)
            except Exception as exc:
                self._log("warning", f"总结对话记忆失败 session={session_id}: {exc}")
                continue
            if flushed is not None:
                outcomes.append(flushed)
        for outcome in outcomes:
            await self._deliver(outcome)
        return outcomes

    @session_scoped
    async def _tick_session(self, session_id: str) -> TickOutcome | None:
        outcome = TickOutcome(session_id=session_id)
        # 自主行为默认落在这一组的代表会话；动作续说会临时改成"她当时说话的地方"
        outcome.speech_home = session_id
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.world_time += 1
            if not self.world.is_home_zone(state.node_id):
                # 人就在外面：一直记着"最近出过门"，回家以后那句提醒才准
                state.last_outdoor_at = self._now()

            # 正在做的动作被停用了：立刻停下（否则"停用"看起来没生效）
            current = state.current_action or {}
            current_def = self.world.action_map().get(str(current.get("type") or ""))
            if current and current_def is not None and not current_def.enabled:
                state.current_action = None
                state.state = STATE_IDLE
                await self._log_event(
                    state,
                    "skip",
                    {
                        "action": current_def.id,
                        "note": f"「{current_def.name or current_def.id}」已停用，动作中止",
                    },
                )

            # 1) 推进持续动作
            await self._tick_continuous(state, node, outcome)

            # 2) 计划推进（无 LLM）
            await self._tick_plan(state, node, outcome, depth=0)

            # 2.6) 临睡期：安静够了（或拖太久了）就真的去睡；否则这一轮什么都不做
            if state.state == STATE_DROWSY and state.current_action is None:
                await self._drowsy_tick(state, node, outcome)

            # 2.5) 刚到新地方、手上又没安排：就地决定接下来做什么
            #      ——只在她真的空下来、而且计划里也没有下一步时才问；
            #      "移动后本来就有动作"（同一串动作或计划里的下一步）不该再问一次
            if state.pending_arrival and state.current_action is None:
                if self._plan_has_pending_step(state):
                    state.pending_arrival = False
                    outcome.notes.append("落地后计划里还有下一步，不再额外决策")
                else:
                    state.pending_arrival = False
                    await self.decide_after_arrival(
                        state, self.node(state.node_id) or node, outcome
                    )

            # 3) 数值演化
            #    扩展包先走一拍（数值、状态结算），再走主插件自己的演化
            try:
                notes = await self.extensions.on_tick(state, self.world, self._now())
                outcome.notes.extend(notes)
            except Exception as exc:
                self._log("warning", f"扩展这一拍出错：{exc}")
            was_storm = bool(state.storm)
            # 新的一天：掷一次今天的基调（曲线快慢），一天只掷一次
            if self._roll_day_mood(state, self._now()):
                info = day_mood_info(state.day_mood)
                await self._log_event(
                    state,
                    "day_mood",
                    {
                        "day_mood": state.day_mood,
                        "label": info.label if info is not None else "",
                        "hint": info.hint if info is not None else "",
                    },
                )
            mood_reset = self.dynamics.tick(
                state,
                node=node,
                elapsed_seconds=self.tick_seconds,
                world=self.world,
                now=self._now(),
            )
            state.mood = self.dynamics.derive_mood(state)
            # 好感每天朝 0 回落一点（长期不理就慢慢淡；一个会话组一天只结算一次）
            decayed = self.profiles.decay_affinity(state.session_id, now=self._now())
            if decayed:
                await self._log_event(
                    state,
                    "affinity_decay",
                    {"count": len(decayed), "step": self.world.profile.affinity_decay_per_day},
                )
            # 「有点想他」：跟群热闹无关，只跟"多久没跟这个人说话"有关
            self._update_miss(state)
            # 到点的回访：想起一件事 → 接下来这一轮她可以顺口提，也可以去找那个人
            review = self.review_block(state)
            if review:
                state.pending_review = review
            # 睡着了：这段时间的经历该消化了（小睡只做轻整理）
            await self.maybe_consolidate(state)
            if mood_reset:
                await self._log_event(state, "mood_reset", {"valence": round(state.valence, 3)})
            if bool(state.storm) != was_storm:
                await self._log_event(
                    state,
                    "storm",
                    {"on": bool(state.storm), "valence": round(state.valence, 3)},
                )
            await self._record_history(state, node)

            # 3.2) 熬夜代价：夜里醒着，或者为了事件推迟了睡觉，精力掉得更快
            penalty = self._stay_up_penalty(state)
            if penalty > 1.0:
                extra = (
                    float(self.world.state_dynamics.energy_decay_per_min)
                    * (penalty - 1.0)
                    * (self.tick_seconds / 60.0)
                )
                state.energy = max(0.0, float(state.energy) - max(0.0, extra))
            if state.stay_up_until and state.world_time > int(state.stay_up_until):
                state.stay_up_until = 0

            # 3.5) 事件系统：她自己遇上的事、以及等群友拿主意这件事
            #      （生成与结算走打杂模型，只有"她怎么选"用主模型；判定是纯计算）
            try:
                await self._maybe_run_event(state, node, outcome)
            except Exception as exc:
                self._log("warning", f"事件系统出错：{exc}")

            # 4) 群聊留档攒太多时压成摘要（只在配置成"压缩"时才会跑）
            await self._maybe_compress_chat(state, outcome)

            # 4.5) 心事随时间淡掉；到期就放下（写进记忆）
            self._decay_heart_knots(state, now=self._now())
            # 4.6) 她记着的账、她自己的事：都到点就放下（账会写进记忆）
            self._decay_grudges(state, now=self._now())
            self._decay_own_topics(state, now=self._now())

            # 4) 无人回应保护
            was_cooling = bool(state.cooldown_until and state.world_time < state.cooldown_until)
            before_unanswered = int(state.unanswered_count or 0)
            verdict = self.engagement.evaluate(state, tick_seconds=self.tick_seconds)
            if int(state.unanswered_count or 0) > before_unanswered:
                # 主动说话没人理 = 被冷落：只压心情，不抬心潮（否则她会从退缩跳成发作）
                magnitude = self.dynamics.ignored_magnitude(state, now=self._now())
                if magnitude:
                    self.dynamics.apply_event(
                        state, "ignored", magnitude=magnitude, now=self._now()
                    )
            if verdict.in_cooldown and not was_cooling:
                await self._log_event(
                    state,
                    "engagement",
                    {
                        "reason": (
                            f"连续 {verdict.unanswered_count} 次主动说话没人回应，"
                            f"安静 {int(self.world.engagement.cooldown_after_unanswered)} 分钟"
                        )
                    },
                )

            # 5) 极端保护
            tick = max(1, self.tick_seconds)
            flags = self.dynamics.check_extremes(
                state,
                low_energy_ticks=int(3 * 86400 / tick),
                high_loneliness_ticks=int(24 * 3600 / tick),
            )
            if (
                flags
                and state.current_plan is None
                and state.current_action is None
                and self._forced_plan_allowed(state)
            ):
                for flag in flags:
                    plan = self.decider.forced_plan(state, flag)
                    if (
                        plan is not None
                        and self.plan_speaks(plan)
                        and self.engagement.proactive_blocked(state)
                    ):
                        # 刚回过话说明有人理她，别再触发"太久没人说话，强制找人"
                        continue
                    if plan:
                        state.current_plan = plan
                        state.last_forced_plan_at = self._now()
                        state.last_forced_flag = flag
                        self._count_autonomous(state)
                        state.add_event("extreme", {"flag": flag})
                        await self._log_event(state, "extreme", {"flag": flag})
                        break

            # 6) 群名片同步
            await self._sync_nickname(state, node)

            if state.current_action or state.current_plan:
                node = self.node(state.node_id) or node
            outcome.notes.append(
                f"t={state.world_time} node={state.node_id} state={state.state} mood={state.mood}"
            )
            self._filter_proactive_speech(outcome)
            if outcome.messages:
                # 她这一轮主动开口了，也算回应过了这些群聊内容
                self.mark_chat_replied(state, outcome.session_id)
            for target in list((outcome.routed or {}).keys()):
                # 说给别处的那几句，也让那个会话的留言算"回应过"
                self.mark_chat_replied(state, target)
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    async def _tick_continuous(
        self, state: WorldState, node: NodeDef | None, outcome: TickOutcome
    ) -> None:
        action = state.current_action
        if not isinstance(action, dict):
            return
        action["elapsed_ticks"] = int(action.get("elapsed_ticks", 0)) + 1
        if int(action.get("elapsed_ticks", 0)) < int(action.get("duration_ticks", 0) or 0):
            return
        await self._finish_action(
            state, node, outcome, action, depth=int(action.get("depth") or 0)
        )

    async def _start_live_call(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        action: PlannedAction,
        payload: dict[str, Any],
    ) -> None:
        """持续型的工具 / 指令动作：开始时就把活儿干完，结果留在 payload 里等收尾。

        好处是图片、指令结果在"她刚说要拍"之后立刻发出来，而持续时间只负责占位；
        收尾时用留下来的结果补一句话，句子就不会赶在图片前面。
        """

        try:
            if definition.llm_level == "tool":
                await self._run_tool_calls(state, definition, payload, outcome=outcome)
                if definition.id == "check_weather":
                    await self._store_weather(state, definition, payload, source="manual")
                payload["tool_live"] = True
                # 结果一拿到就记进状态槽：持续动作还要占位一段时间，
                # 没必要等收尾——那段占位时间里提示词也该看得到「今天穿的是…」
                if await self._remember_external_state(
                    state, definition, payload, outcome
                ):
                    payload["slot_recorded"] = True
                return
            info: dict[str, Any] = {}
            await self._run_command_action(
                state,
                node,
                outcome,
                definition,
                PlannedAction(
                    type=definition.id,
                    intent=action.intent,
                    content=action.content,
                    target=action.target,
                    target_node=action.target_node,
                    params=dict(action.params or {}),
                ),
                out=info,
                speak=False,
            )
            if info:
                payload["cmd_cache"] = info
                payload["tool_live"] = True
                if await self._remember_external_state(
                    state, definition, payload, outcome
                ):
                    payload["slot_recorded"] = True
        except Exception as exc:
            # 这次没跑成不算完：不标 tool_live，收尾时会照常再走一遍
            self._log("warning", f"「{definition.id}」的即时调用失败：{exc}")
            await self._log_event(
                state,
                "skip",
                {"action": definition.id, "note": f"即时调用出错，稍后再试：{exc}"},
                outcome=outcome,
            )

    @speech_origin("completion")
    async def _finish_action(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: dict[str, Any],
        *,
        depth: int = 0,
    ) -> bool:
        action_id = str(action.get("type", ""))
        definition = self.world.action_map().get(action_id)

        # 这个动作是在哪个会话里被安排的：续说/结果那句话回那儿说。
        # （私聊里让她做饭，做完那句就回私聊，不会跑到群里）
        previous_home = outcome.speech_home
        action_home = str(action.get("session") or "")
        if action_home:
            outcome.speech_home = action_home

        handled_followup = False
        if action_id == "walk_to":
            target_node = str(action.get("target_node", "") or "")
            if target_node and target_node in self.world.node_map():
                # 离开这个地点之前，把刚才在那儿聊的那段总结成一条记忆
                if bool(self.world.memory.summary_on_move):
                    state.memory_flush_wanted = True
                state.node_id = target_node
                # 换地方了：事件系统按"在这里待了多久"决定要不要出事，重新计时
                state.node_since = self._now()
                if not self.world.is_home_zone(target_node):
                    # 出门了：记一笔，久不出去会想出去走走（见 _outdoor_hint）
                    state.last_outdoor_at = self._now()
                state.add_event("move", {"to": target_node})
                # 特地走到一个新地方，落地后就地看看这里能做什么
                if int(action.get("arrival_decide", 1)):
                    state.pending_arrival = True
        elif definition is not None and definition.llm_level == "tool":
            # 工具型动作：配了几个工具就按顺序调几个，成功失败都记账，
            # 日志页要能看出"她到底查到了什么 / 为什么没查到"。
            # 持续型动作在开始的那一刻就调过了（``tool_live``），这里不再调第二次。
            if action.get("tool_live"):
                outcome.notes.append(
                    f"「{definition.name or definition.id}」的结果在开始时就拿到了，占位时间结束"
                )
            else:
                await self._run_tool_calls(state, definition, action, outcome=outcome)
                if definition.id == "check_weather":
                    # 查到的天气顺带进全局记录：提示词、编辑器横幅、静默刷新都读它
                    await self._store_weather(state, definition, action, source="manual")
        elif definition is not None and definition.llm_level == "command":
            # 持续型的指令动作：和工具型动作一样，到点才把指令发出去。
            # 以前这里没有这一支，"查天气"这种配成持续动作的指令永远不执行、也一条日志都没有。
            # 开始时就跑过的（``tool_live``）只用当初的结果补一句话，不再执行第二次。
            await self._run_command_action(
                state,
                node,
                outcome,
                definition,
                PlannedAction(
                    type=action_id,
                    intent=str(action.get("intent") or ""),
                    content=str(action.get("content") or ""),
                    target=str(action.get("target") or ""),
                    target_node=str(action.get("target_node") or ""),
                    params=dict(action.get("params") or {}),
                ),
                cached=dict(action.get("cmd_cache") or {}) or None,
                remember=action if isinstance(action, dict) else None,
            )
            handled_followup = True

        effects = (definition.on_complete.effects if definition else {}) or {}
        if effects:
            self.dynamics.apply_effects(state, effects, world=self.world, now=self._now())
        # 持续型亲密动作（靠着、按摩这类）做完也算安抚，同上
        await self._maybe_soothe_from_action(state, definition, outcome=outcome)

        # 查完东西的"满足感"：检索型动作真拿到结果之后，好奇心按动作配置回落一次
        if definition is not None:
            self._satisfy_curiosity(state, definition, action, outcome)
            # 亲近了一下：抱一抱、摸摸头这种**真的碰到**的动作会满足欲求
            await self._satisfy_desire_from_action(state, definition, outcome)

        # 按「实际持续时间」缩放的效果：例如小睡 30 分钟 → 精力 +0.002×30
        per_minute = (definition.on_complete.effects_per_minute if definition else {}) or {}
        if per_minute:
            elapsed_seconds = int(action.get("elapsed_ticks", 0)) * self.tick_seconds
            minutes = max(0.0, elapsed_seconds / 60.0)
            if minutes > 0:
                self.dynamics.apply_effects(
                    state, per_minute, world=self.world, scale=minutes, now=self._now()
                )

        if definition is not None and definition.id == "sleep":
            # 睡整觉结束：睡了多久、中途被吵醒过没有，决定她是清爽还是起床气
            self._apply_wake_quality(
                state,
                slept_minutes=int(action.get("elapsed_ticks", 0)) * self.tick_seconds / 60.0,
                startled=int(state.startled_count or 0),
            )
            state.sleep_started_at = 0
            # 睡醒给她一次机会说早安（发不发、发给谁由她自己定，一天一次）
            await self._maybe_say_goodmorning(state, node, outcome)

        # 行动完成 -> 记忆
        if definition is not None:
            self._remember_action(state, definition, action)

        state.current_action = None
        state.state = STATE_IDLE
        if action.get("from_plan") and state.current_plan is not None:
            plan = state.current_plan
            recorded = action.get("plan_created_at")
            if recorded is None:
                # 没记下自己属于哪份计划（老存档、别的来源起跑的）：保持原来的做法
                advance(state)
            else:
                try:
                    same_plan = int(plan.get("created_at") or 0) == int(recorded)
                    same_step = plan.get("current_step") is not None and int(
                        plan.get("current_step")
                    ) == int(action.get("plan_index", -1))
                except (TypeError, ValueError):
                    same_plan = same_step = False
                if same_plan and same_step:
                    # 计划还是带它起跑的那一份、也还指着它：推掉这一步
                    advance(state)
                else:
                    # 计划中途换过 / 已经有新的安排排到前面：这一步不是计划的了，
                    # 不能顺手把排在她前面的动作标成"做过了"
                    outcome.notes.append("计划已经有别的安排了，这一步不再往后推")

        await self._log_event(
            state,
            "action_done",
            {
                "type": action_id,
                "elapsed_ticks": int(action.get("elapsed_ticks", 0)),
                "tool_result": _clip_text(action.get("tool_result"), 200),
            },
            outcome=outcome,
        )

        trigger = definition.on_complete.trigger if definition else "none"
        detail = str(action.get("tool_result", "") or "")
        evidence_text = self._search_evidence_text(definition, action)
        digest = str(action.get("tool_digest") or "").strip()
        # 别的插件的状态（穿搭、背包…）：把结果记进状态槽，之后每轮都带着
        # （开始时已经记过的就别再记一遍，免得槽里的时间被推到收尾那一刻）
        if not action.get("slot_recorded"):
            await self._remember_external_state(state, definition, action, outcome)
        if digest:
            # 检索型动作交回主模型的是"要点+编号"，不是一堆原文
            detail = digest
        elif evidence_text:
            # 没压出要点时退回编号证据块，也不是被截断的一坨原文
            detail = evidence_text
        images = list(action.get("tool_images") or [])
        if not detail and images:
            detail = "工具返回了一张图片，图片一起发给你了。"
        hint = definition.on_complete.prompt_hint if definition else ""
        if (digest or evidence_text) and not str(hint or "").strip():
            hint = (
                "把查到的内容讲给群里听：只能依据上面这些材料，"
                "不要用印象补充、不要编数字或时间；材料里没有就直说没查到。"
                "别照抄原文、别念网址，用你自己的口吻挑最有用的两三点。"
            )
        if (digest or evidence_text) and bool(getattr(definition, "search_cite", False)):
            hint = (
                str(hint or "").rstrip()
                + " 讲完可以在末尾用括号补一条你参考的来源链接。"
            )
        want_followup = trigger == "llm_followup"
        is_tool_action = bool(definition is not None and definition.llm_level == "tool")
        tool_failed = is_tool_action and not bool(action.get("tool_ok", True))
        if handled_followup:
            # 指令动作自己已经把结果交回给她说过一句了，这里别再问一次
            want_followup = False
        if want_followup and is_tool_action and not detail:
            failure = str(action.get("tool_error") or "").strip()
            if failure:
                # 没做成也要说一句：把**失败原因**交给她，让她照实讲，
                # 别让这一步静默消失（用户会以为她根本没试）。
                detail = f"（这一步没做成：{failure}）"
                hint = (
                    hint
                    or "刚才那件事没做成，用一句自然的话说明一下，别编内容、别道歉过头。"
                )
                want_followup = True
            else:
                # 工具真的一点东西都没回：别让她"就着空气说话"——那只会编
                want_followup = False
                outcome.notes.append(
                    f"工具动作 {action_id} 没有拿到结果（结果为空），不作续说"
                )
        if (
            not want_followup
            and trigger == "none"
            and not tool_failed
            and detail
            and is_tool_action
            and not action.get("reply_planned")
            and bool(self.world.tool_result_reply)
        ):
            # 工具型动作拿到结果后，默认交回主模型说一句（可以用全局设置关掉）
            want_followup = True
            hint = hint or "把刚才查到的结果用你自己的话说出来，简短自然，别念成清单。"
        if want_followup and self._silent_followup:
            # 事件里"先去做点什么"那一轮：结果只交回给她判断，不往群里说
            want_followup = False
            outcome.notes.append(f"动作 {action_id} 的结果只用于事件判断，不作续说")
        followup_spoke = False
        if want_followup and not self.speech_allowed(outcome.home(), outcome.speech_kind):
            want_followup = False
            outcome.notes.append("自主搭话已暂停，动作照常完成，不作自主续说")
        if want_followup and self.llm is not None:
            if not detail:
                # 不是工具动作（做饭、看书…）也要能续说：给她一句"刚刚做了什么"的交代
                minutes = max(
                    0, round(int(action.get("elapsed_ticks", 0)) * self.tick_seconds / 60)
                )
                label = definition.name or definition.id if definition else ""
                detail = f"你刚刚做完了「{label}」，用了大约 {minutes} 分钟。"
            detail = self._clip_followup(detail)
            followup_spoke = await self._llm_followup(
                state,
                node,
                outcome,
                hint,
                detail,
                image_urls=images,
                depth=depth + 1,
                after_search=bool(
                    definition is not None
                    and str(getattr(definition, "tool_flow", "simple")) == "search"
                ),
                definition=definition,
            )
        elif trigger == "schedule":
            await self._run_linked_schedule(state, node, outcome, definition, depth=1)

        if action_home:
            outcome.speech_home = previous_home
        return followup_spoke

    async def _remember_external_state(
        self,
        state: WorldState,
        definition: ActionDef | None,
        action: dict[str, Any],
        outcome: TickOutcome | None = None,
    ) -> bool:
        """把这次的结果记进状态槽（别的插件的状态就靠它留下来）。

        只有配了 ``state_slot`` 的动作才记：留档里那些"今天穿的是白色卫衣"之类的
        东西，下一轮提示词里要继续看得到。

        返回值表示**真的写进去了**（没配槽、没有结果文本时返回 ``False``），
        调用方靠它决定要不要在收尾时再试一次。
        """

        slot = str(getattr(definition, "state_slot", "") or "").strip()
        if not slot or definition is None:
            return False
        raw = str(
            action.get("tool_result")
            or action.get("tool_digest")
            or (dict(action.get("cmd_cache") or {}) or {}).get("text")
            or ""
        ).strip()
        if not raw:
            return False
        text = raw
        if bool(getattr(definition, "state_summarize", False)) and self.helper_llm is not None:
            summary = await self._summarize_state_text(state, definition, raw)
            if summary:
                text = summary
        label = str(getattr(definition, "state_label", "") or "").strip() or slot
        try:
            ttl = max(0, int(getattr(definition, "state_ttl_minutes", 0) or 0))
        except (TypeError, ValueError):
            ttl = 0
        now = self._now()
        state.external_state = {
            **dict(getattr(state, "external_state", None) or {}),
            slot: {
                "text": _clip_text(text, 400),
                "label": label,
                "at": now,
                "expires_at": (now + ttl * 60) if ttl else 0.0,
            },
        }
        await self._log_event(
            state,
            "state_slot",
            {"slot": slot, "label": label, "text": _clip_text(text, 200)},
            outcome=self._echo_into(outcome, state),
        )
        return True

    async def _summarize_state_text(
        self, state: WorldState, definition: ActionDef, raw: str
    ) -> str:
        """让打杂模型把结果压成一句话（返回是一大段时用）。"""

        label = str(getattr(definition, "state_label", "") or definition.name or "").strip()
        try:
            reply = await self._ask_helper(
                state.session_id,
                "你把一段结果压成一句话，给角色自己看。只输出这句话本身，"
                "保留具体内容（颜色、名称、数字），不要客套、不要解释。",
                f"这是「{label or '当前状态'}」的内容：\n{_clip_text(raw, 1200)}\n\n"
                "用一句话说清它现在是什么。",
            )
        except Exception as exc:
            self._log("debug", f"压缩状态槽失败：{exc}")
            return ""
        return " ".join(str(reply or "").split())[:200]

    async def _run_linked_schedule(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        definition: ActionDef,
        *,
        depth: int,
    ) -> None:
        """``on_complete.trigger = schedule``：动作做完后接着跑另一个日程的动作链。"""

        schedule_id = (definition.on_complete.schedule_id or "").strip()
        if not schedule_id or self.schedules is None:
            return
        target = next(
            (item for item in self.schedules.schedules if item.id == schedule_id), None
        )
        if target is None:
            outcome.notes.append(f"找不到后续日程 {schedule_id}")
            return
        if depth > self.world.limits.max_action_chain_depth:
            outcome.notes.append("动作链超过深度上限，已停止")
            return
        state.add_event("chain", {"schedule": schedule_id})
        await self._run_chain(state, target.action_chain, outcome, depth=depth)

    def _remember_inner_thought(
        self, state: WorldState, text: str, source: str = ""
    ) -> None:
        """把这一轮她心里冒出来的那一句留下来（编辑器「内心活动」那一栏）。

        以前只有 ``think`` 动作会写它，而模型很少主动选那个动作，
        于是那一栏动不动停在一句很多轮之前的旧话上。现在两条路都会补一句：
        回合复盘里的 reasoning.inner，和自主决策计划里的 inner。
        同一句连着出现就跳过，免得连着几条一模一样的。
        """

        clean = " ".join(str(text or "").split()).strip()[:120]
        if not clean:
            return
        recent = [
            str(item.get("content") or "").strip()
            for item in state.thoughts[-3:]
            if isinstance(item, dict)
        ]
        if clean in recent:
            return
        state.add_thought(clean, source=source)

    def _remember_action(
        self, state: WorldState, definition: ActionDef, action: dict[str, Any]
    ) -> None:
        label = definition.name or definition.id
        if definition.id == "think":
            content = str(action.get("content", "") or "")
            if content:
                state.add_thought(content, source="她想事情")
                self.memory.remember(
                    session_id=state.session_id,
                    persona_id=str(action.get("persona_id", "")),
                    node_id=state.node_id,
                    content=content,
                    memory_type=INNER,
                    emotion=state.mood,
                    weight=0.35,
                    affect=state.affect,
                    valence=state.valence,
                )
            return
        self.memory.remember(
            session_id=state.session_id,
            persona_id=str(action.get("persona_id", "")),
            node_id=state.node_id,
            content=f"在这里{label}",
            memory_type=SCENE,
            emotion=state.mood,
            weight=0.3,
            affect=state.affect,
            valence=state.valence,
        )

    async def _llm_followup(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        hint: str,
        tool_result: str,
        image_urls: list[str] | None = None,
        *,
        depth: int = 1,
        after_search: bool = False,
        definition: Any = None,
    ) -> bool:
        persona_text = await self._persona_text(state.session_id)
        _cell, say_limit, style_text = self.style_for(state, state.session_id)
        blocked_actions = self._hidden_actions(state)
        # 续说落在哪个会话里：它同时决定"取哪一份聊天窗口"和"当前会话是谁"
        home = outcome.home()
        system_prompt = self.prompts.build_autonomous_system_prompt(
            persona_text=persona_text,
            state=state,
            node=node,
            available_tools=self.available_tools(),
            memories=self.memory.recall(
                session_id=state.session_id,
                persona_id="",
                node_id=state.node_id,
                limit=3,
            ),
            engagement_hint=self.engagement.hint(state),
            max_messages=say_limit,
            # 取窗口的会话必须**和 current_session 是同一个**：不然这里按代表会话
            # 的额度取（目标会话的话被挤进"别处听到的"），她续说时聊的根本不是
            # 自己刚在说话的那个地方，已回应过的历史也补不齐。
            recent_chat=self.chat_window(state, home),
            reasoning=bool(self.world.reasoning_enabled),
            style_block=style_text,
            hidden_actions=blocked_actions,
            session_directory=self.session_directory(state),
            # 她此刻是在哪个会话里说话：这里/别处的划分、水位线都按它算
            current_session=home,
            session_labels=self.session_labels(state),
            profile_text=self.profile_block(state),
            extra_notes=[note for note in [self.extra_reminders(state)] if note],
            json_fields=self.extensions.json_fields(),
            **await self.runtime_notes(state.session_id),
        )
        prompt = self.prompts.build_reply_followup_prompt(
            hint,
            tool_result,
            no_search=after_search,
            event_digest="；".join(
                str(item)
                for item in list(state.event_digest or [])[-4:]
                if str(item).strip()
            ),
        )
        # 用过了就清掉：这段时间发生的事只带出来一次
        state.event_digest = []
        reply = await self._ask_llm(
            state.session_id,
            system_prompt,
            prompt,
            image_urls=list(image_urls or []) or None,
        )
        outcome.llm_calls += 1
        if reply is None:
            return False
        result = parse_action_payload(
            reply,
            available_actions=self._parseable_action_ids(
                state.node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=say_limit,
            json_fields=self.extensions.json_fields(),
        )
        actions, blocked = self._followup_actions(
            result.actions, depth=depth, after_search=after_search
        )
        # 「她在看什么 / 在玩什么」这类内容：不是工具返回的，是她自己刚说出来的。
        # 配了状态槽的动作把它记下来，之后每一轮提示词里都带着——不然每次追剧
        # 都像第一次看，只会凭空吐槽（"这剧情也太离谱了"，但你不知道她在看什么）。
        self._note_action_state_slot(
            state,
            definition,
            [text for item in actions if item.type == "say" for text in (item.messages or [])],
        )
        if blocked:
            await self._log_event(
                state,
                "skip",
                {
                    "action": "、".join(blocked),
                    "note": "续说这一轮不再接新的检索 / 工具动作（刚查完或链条太深），只让她说话",
                },
                outcome=outcome,
            )
        before = len(outcome.said_messages())
        await self._execute_actions(
            state, node, outcome, actions, depth=depth, autonomous=True
        )
        return len(outcome.said_messages()) > before

    def _note_action_state_slot(
        self, state: WorldState, definition: Any, said: list[str]
    ) -> bool:
        """把"她刚说的那句"记进动作声明的状态槽（非工具型动作用）。

        追剧、看书、打游戏这类动作没有工具返回值，内容是**她自己定的**。
        记下来之后提示词里就有"在看的剧：…"，她不会每次都对同一个剧重新开始。
        """

        slot = str(getattr(definition, "state_slot", "") or "").strip()
        if not slot or definition is None:
            return False
        text = " ".join(" ".join(str(item or "").split()) for item in said if str(item or "").strip())
        if not text:
            return False
        label = str(getattr(definition, "state_label", "") or "").strip() or slot
        try:
            ttl = max(0, int(getattr(definition, "state_ttl_minutes", 0) or 0))
        except (TypeError, ValueError):
            ttl = 0
        now = self._now()
        state.external_state = {
            **dict(getattr(state, "external_state", None) or {}),
            slot: {
                "text": _clip_text(text, 200),
                "label": label,
                "at": now,
                "expires_at": (now + ttl * 60) if ttl else 0.0,
            },
        }
        return True

    def _followup_actions(
        self, actions: list[Any], *, depth: int, after_search: bool
    ) -> tuple[list[Any], list[str]]:
        """续说那一轮能用哪些动作。

        两条闸门：
        - **刚查完就别再查**：不然她会"查一段、说一段、再查一段"，一路查下去；
        - **链条太深就只让她说话**：以前每轮都从 depth=1 重新起链，等于没有上限。
        """

        limit = int(self.world.limits.max_action_chain_depth)
        too_deep = depth > limit
        kept: list[Any] = []
        blocked: list[str] = []
        for item in actions:
            definition = self.world.action_map().get(str(getattr(item, "type", "")))
            is_search = (
                definition is not None
                and str(getattr(definition, "tool_flow", "simple")) == "search"
            )
            is_tool = definition is not None and definition.llm_level in ("tool", "command")
            if (after_search and is_search) or (too_deep and is_tool):
                blocked.append(str(getattr(item, "type", "")))
                continue
            kept.append(item)
        if blocked:
            self._log(
                "debug",
                f"续说这一轮不接新的检索/工具动作：{'、'.join(blocked)}",
            )
        return kept, blocked

    @speech_origin("plan")
    async def _tick_plan(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        *,
        depth: int,
    ) -> None:
        if depth > self.world.limits.max_action_chain_depth:
            return
        if state.state == STATE_DROWSY:
            # 临睡期里计划不推进：她正打着盹，"睡觉"那一步已经揣在手里了，
            # 这时候放计划往前走就等于当晚没打盹——下一拍又直接躺下了。
            return
        if state.current_action is not None:
            return
        step = peek_step(state)
        if step is None:
            return
        action = self._step_to_action(step)
        step_home = str(step.get("session") or "")
        previous_home = outcome.speech_home
        if step_home:
            outcome.speech_home = step_home
        if str(step.get("kind") or "") == "reply":
            # 这一句是当时有人跟她说话时排下的：轮到时按"回话"发，不算她主动开口
            outcome.speech_kind = "reply"
        executed = await self._execute_actions(
            state,
            node,
            outcome,
            [action],
            depth=depth,
            autonomous=True,
            from_plan=True,
            # 日程排下来的步骤同样不算"她自己想找他"
            from_schedule=str((active_plan(state) or {}).get("source") or "") == "schedule",
        )
        outcome.speech_home = previous_home
        if not executed:
            # 这一步被跳过了（地点/前置条件/缺工具…）。不推进的话它会每个 tick 重试一次。
            outcome.notes.append(f"计划里的「{action.type}」没做成，跳过这一步")
            advance(state)
