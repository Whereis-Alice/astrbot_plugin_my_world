"""天气：拉取、缓存与提示词那一行。"""

from __future__ import annotations

from typing import Any
from ..defaults import DEFAULT_WEATHER_PROMPT
from ..json_actions import (
    PlannedAction,
)
from ..models import (
    ActionDef,
)
from ..state import (
    WorldState,
)
from ..weather import (
    WEATHER_KEY,
    WEATHER_TRY_KEY,
    WeatherRecord,
    city_from_tool_args,
    looks_like_weather_text,
    looks_like_weather_tool,
    parse_parts,
    parts_for,
    weather_tool_score,
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
        """已注册的工具里，哪些像是查天气的（按名字和说明认，不写死工具名）。

        名字里带 weather / 天气 的排在前面，只在说明里提过天气的排后面——
        分析、日报类工具的说明里也常提到"天气"，排前面会把它们认成天气源。
        """

        found: list[tuple[int, str]] = []
        for name, description in (self.available_tools() or {}).items():
            score = weather_tool_score(name, description)
            if score > 0:
                found.append((score, str(name)))
        found.sort(key=lambda item: (-item[0], item[1]))
        return [name for _score, name in found]

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
        if not looks_like_weather_text(text):
            # 工具认错了（例如说明里提过"天气"的分析类工具）：宁可不记，
            # 也别把群聊分析报告写成她的天气（见 #9）
            self._log(
                "debug",
                f"这条工具结果不像天气，没有记下来：{_clip_text(text, 120)}",
            )
            return WeatherRecord()
        city = city_from_tool_args(action.get("params"))
        parts = parts_for(text, city) or parts_for(raw, city)
        record = WeatherRecord(
            text=text,
            parts=parts,
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
