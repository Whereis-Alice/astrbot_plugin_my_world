"""工具调用与联网检索流水线。"""

from __future__ import annotations

from ..model_calls import model_operation

import asyncio
import time
from typing import Any
from ..json_actions import (
    PlannedAction,
    extract_json_object,
)
from ..models import (
    ActionDef,
    action_intimacy,
)
from ..ports import ToolCallResult
from ..search import (
    Evidence,
    clean_passage,
    evidence_text,
    failed_text,
    looks_like_homepage,
    looks_like_nav,
    merge_evidence,
    parse_search_results,
    queries_too_similar,
    render_evidence,
    sources_of,
    unwrap_payload_text,
)
from ..state import WorldState
from .constants import (
    DEFAULT_SEARCH_TOPIC,
    READ_CACHE_SECONDS,
    READ_PASSAGE_CHARS,
    SEARCH_BUDGET_SECONDS,
    SEARCH_QUERY_RULES,
)
from .models import TickOutcome
from .util import (
    _clean_search_topic,
    _clip_log,
    _clip_text,
    _looks_like_argument_error,
    normalize_param_schema,
)


class SearchMixin:
    async def _run_tool_calls(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """按配置顺序调用动作挂着的工具，把结果汇总进 ``action``。"""

        names = self.resolve_tools(definition, state.node_id)
        if str(getattr(definition, "tool_flow", "simple")) == "search":
            # 联网检索形态走自己的流水线（多查询 / 证据 / 读正文 / 补查）
            await self._run_search_flow(
                state, definition, action, names, outcome=outcome
            )
            return
        mode = str(getattr(definition, "tool_mode", "sequence") or "sequence")
        stored = dict(action.get("tool_params") or {})
        texts: list[str] = []
        failed: list[str] = []
        images: list[str] = []
        last_params: dict[str, Any] = {}
        used: list[str] = []
        if mode == "smart" and len(names) > 1:
            await self._run_tools_smart(
                state,
                definition,
                action,
                names,
                outcome=outcome,
                texts=texts,
                failed=failed,
                images=images,
                used=used,
            )
        else:
            for index, name in enumerate(names):
                entry = await self._call_one_tool(
                    state,
                    definition,
                    action,
                    name,
                    params=dict(stored.get(name) or action.get("params") or {}),
                    previous_results="\n\n".join(texts) if index > 0 else "",
                    outcome=outcome,
                )
                if entry is None:
                    failed.append(f"{name}：缺少参数")
                    continue
                call, sent = entry
                used.append(call.tool or name)
                if not last_params:
                    last_params = sent
                if call.ok and call.text:
                    texts.append(str(call.text))
                elif not call.ok:
                    failed.append(f"{call.tool or name}：{call.error or '没有返回结果'}")
                images.extend(getattr(call, "image_urls", None) or [])
                # 结果里带回来的图要发出去（生图这类动作的意义就在这里）：
                # 落点跟着这一步的正文走，别一律塞回这一拍的会话
                attachments = list(getattr(call, "attachments", None) or [])
                landing = self._image_home(outcome, action)
                self._attach_images(outcome, attachments, session_id=landing)
                if attachments:
                    self._note_own_image(
                        state,
                        str(action.get("intent") or action.get("content") or definition.name),
                        landing,
                    )
                await self._log_tool_outcome(
                    state, definition, name, call, outcome=outcome
                )
                if mode == "fallback" and call.ok and (call.text or images):
                    # 依次尝试：这一个跑通了就不再碰后面的
                    break

        action["tool_names"] = list(names)
        action["tool_name"] = names[-1] if names else ""
        action["tool_used"] = used
        # 只有图片也算拿到结果：不然"生成一张图"这类工具会被当成失败
        action["tool_ok"] = bool(texts or images)
        action["tool_error"] = "；".join(failed)
        if images:
            action["tool_images"] = images
        if texts:
            action["tool_result"] = "\n\n".join(texts)
        elif last_params:
            action.setdefault("params", last_params)
    def _action_base_params(self, definition: ActionDef, action: Any) -> dict[str, Any]:
        """动作上配的固定参数打底，这一轮模型给的参数覆盖在它上面。"""

        fixed: dict[str, Any] = {}
        for key, spec in (definition.params or {}).items():
            value = getattr(spec, "value", "")
            if isinstance(value, str) and value.strip():
                fixed[str(key)] = value.strip()
        return {**fixed, **dict(self._act_get(action, "params", {}) or {})}

    def _explicit_queries(
        self, definition: ActionDef, action: Any, *, limit: int = 0
    ) -> list[str]:
        """她自己写进动作里的查询词（没写就是空列表，交给调用方兜底）。"""

        cap = int(limit or getattr(definition, "search_max_queries", 3) or 1)
        cap = max(1, min(cap, 5))
        result: list[str] = []
        for item in self._act_get(action, "queries", []) or []:
            text = " ".join(str(item).split())[:120]
            if text and text not in result:
                result.append(text)
            if len(result) >= cap:
                break
        return result

    def _search_intent(self, action: Any) -> str:
        """她自己写明的"想查什么"。"""

        return str(
            self._act_get(action, "intent", "")
            or self._act_get(action, "content", "")
            or ""
        ).strip()

    def _search_topic(self, definition: ActionDef, action: Any) -> str:
        """她已经写明的意图 / 动作里配好的主题 / 查询模板；都没有就返回空串。

        注意别拿动作说明兜底：那写的是"这个动作怎么用"，不是"要查什么"。
        真正的兜底（让大模型自己想一句、或者中性主题）在 ``_search_intent_for`` 里。
        """

        intent = self._search_intent(action)
        if intent:
            return intent
        topic = str(getattr(definition, "search_topic", "") or "").strip()
        template = str(getattr(definition, "search_query_template", "") or "").strip()
        if template:
            date_text = self.local_now().strftime("%Y-%m-%d")
            text = " ".join(template.replace("{topic}", topic).replace("{date}", date_text).split())
            if text:
                return text[:120]
        if topic:
            return topic
        return ""

    async def _search_intent_for(
        self, state: WorldState, definition: ActionDef, action: Any
    ) -> str:
        """这一轮"想查什么"：她写的 > 配置的主题/模板 > 让她自己想 > 中性兜底。"""

        given = self._search_topic(definition, action)
        if given:
            return given
        generated = await self._ask_search_topic(state)
        return generated or DEFAULT_SEARCH_TOPIC

    @model_operation("search", "准备搜索，需要确定主题")
    async def _ask_search_topic(self, state: WorldState) -> str:
        """规则触发、她自己又没写意图时，问一句"你现在想查什么"。

        问出来的话题贴着她此刻的处境和群里正在聊的事，比固定主题自然；
        没模型 / 额度用完 / 回答不可用时返回空串，由调用方兜底。
        """

        if self.llm is None or not self._llm_text_allowed(state):
            return ""
        node = self.node(state.node_id)
        chat_lines = [
            f"{item.get('name') or item.get('user_id')}：{_clip_text(item.get('text'), 40)}"
            for item in self.chat_window(state)[-6:]
        ]
        system_prompt, prompt = self.prompts.build_search_topic_prompt(
            where=(node.name or node.id) if node else "",
            state_hint=(
                f"心情 {state.mood}，无聊 {state.boredom:.2f}，好奇心 {state.curiosity:.2f}"
            ),
            chat_lines=chat_lines,
            persona_text=_clip_text(await self._persona_text(state.session_id), 400),
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        self._count_llm_text(state)
        return _clean_search_topic(reply)

    def _query_param_name(self, tool: str) -> str:
        """工具里"查询词"该填哪个参数：按名字猜，认不出来就取第一个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        exact = {
            "q",
            "query",
            "queries",
            "keyword",
            "keywords",
            "wd",
            "text",
            "prompt",
            "words",
            "search_query",
            "search_keyword",
        }
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in exact or "query" in low or "keyword" in low:
                    return name
        return required[0] if required else (properties[0] if properties else "")

    @staticmethod
    def _query_from_params(params: dict[str, Any]) -> str:
        """工具 schema 认不出来时，从这一轮已经带上的参数里找一个像查询词的值。

        日程 / 计划里常常直接写了 ``params: {query: ...}``，别让它白写。
        """

        values = [
            str(value).strip()
            for value in (params or {}).values()
            if isinstance(value, str) and str(value).strip()
        ]
        if len(values) == 1:
            return values[0]
        for key, value in (params or {}).items():
            low = str(key).lower()
            if any(token in low for token in ("query", "keyword", "wd")) and str(
                value
            ).strip():
                return str(value).strip()
        return ""

    def _url_param_name(self, tool: str) -> str:
        """阅读工具里"网址"该填哪个参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(tool) or {})
        properties = [str(name) for name in (schema.get("properties") or {})]
        required = [str(name) for name in (schema.get("required") or [])]
        for pool in (required, properties):
            for name in pool:
                low = name.lower()
                if low in ("url", "uri", "link", "href", "address", "webpage", "page"):
                    return name
                if "url" in low or "link" in low:
                    return name
        return required[0] if required else (properties[0] if properties else "")

    def _reader_tools(self, definition: ActionDef, state: WorldState) -> list[str]:
        """这个检索动作配了哪些可用的阅读网页工具。"""

        result: list[str] = []
        for name in definition.reader_tool_names or []:
            chosen = self.resolve_tool(definition.id, state.node_id, name) or (
                self.resolve_tool_prefix(name)
            )
            if chosen and chosen not in result:
                result.append(chosen)
        return result

    @staticmethod
    def _search_budget(
        definition: ActionDef, action: dict[str, Any] | None = None
    ) -> tuple[int, int]:
        """``(最多读几篇正文, 最多补查几轮)``；``quick`` 档查一轮就收工。

        动作里配的是**上限**：她自己写 ``search_depth`` / ``read_pages`` 时可以收着点用，
        但不能超过配置（免得她每次都开深挖）。
        """

        ranks = {"quick": 0, "standard": 1, "deep": 2}
        configured = str(getattr(definition, "search_depth", "standard") or "standard")
        wanted = str((action or {}).get("search_depth") or "").strip().lower()
        depth = configured
        if wanted in ranks and ranks[wanted] < ranks.get(configured, 1):
            depth = wanted
        reads = max(0, int(getattr(definition, "search_max_reads", 2) or 0))
        rounds = max(0, int(getattr(definition, "search_rounds", 1) or 0))
        asked_reads = (action or {}).get("read_pages")
        try:
            if asked_reads is not None and int(asked_reads) >= 0:
                reads = min(reads, int(asked_reads))
        except (TypeError, ValueError):
            pass
        if depth == "quick":
            return 0, 0
        if depth == "deep":
            return max(reads, 3), max(rounds, 2)
        return reads, rounds

    @staticmethod
    def _search_needs_more(items: list[Evidence]) -> bool:
        """证据是不是太薄：条数太少、或者全是短摘要没有正文。"""

        return not SearchMixin._search_sufficient(items)

    @staticmethod
    def _search_sufficient(items: list[Evidence]) -> bool:
        """证据够不够用：至少两条，而且其中一条有像样的正文或摘要。"""

        if len(items) < 2:
            return False
        return any(
            len((item.passage or item.snippet or "").strip()) >= 60 for item in items
        )

    async def _search_once(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        tool: str,
        query: str,
        base: dict[str, Any],
        query_key: str,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
        texts: list[str],
        items: list[Evidence],
        failed: list[str],
        images: list[str],
        used: list[str],
    ) -> ToolCallResult | None:
        """按一条查询词调一次搜索工具，结果直接并进证据。"""

        params = dict(base)
        if query_key:
            params[query_key] = query
        entry = await self._call_one_tool(
            state,
            definition,
            action,
            tool,
            params=params,
            outcome=outcome,
            echo=echo,
        )
        if entry is None:
            failed.append(f"{tool}：缺少参数")
            return None
        call, _sent = entry
        used.append(call.tool or tool)
        if call.ok and call.text:
            texts.append(str(call.text))
            items.extend(parse_search_results(str(call.text), source=query))
        elif not call.ok:
            failed.append(f"{call.tool or tool}：{call.error or '没有返回结果'}")
        images.extend(getattr(call, "image_urls", None) or [])
        await self._log_tool_outcome(
            state, definition, tool, call, outcome=outcome, echo=echo
        )
        return call

    @staticmethod
    def _next_search_tool(tool: str, candidates: list[str]) -> str:
        """下一个候选搜索工具（没有就返回空串）。"""

        if tool not in candidates:
            return ""
        index = candidates.index(tool)
        return candidates[index + 1] if index + 1 < len(candidates) else ""

    async def _search_batch(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        queries: list[str],
        base: dict[str, Any],
        query_key: str,
        tool: str,
        candidates: list[str],
        *,
        outcome: TickOutcome | None,
        texts: list[str],
        items: list[Evidence],
        failed: list[str],
        images: list[str],
        used: list[str],
        asked: list[str],
    ) -> str:
        """并发查一批查询词；整批都因为"工具本身坏了"失败时，换下一个候选重试一遍。

        并发是刻意的：串行时每条要等上一条搜完（一次 3~4 秒），既慢、回显也会散成好几行。
        """

        if not queries:
            return tool
        asked.extend(queries)
        # 检索期间的工具调用**不进群**（只写日志）：这里只留一条「正在联网搜索」
        await self._log_event(
            state,
            "search",
            {"action": definition.id, "queries": list(queries)},
            outcome=outcome,
        )
        for _ in range(len(candidates) + 1):
            results = await asyncio.gather(
                *(
                    self._search_once(
                        state,
                        definition,
                        action,
                        tool,
                        query,
                        base,
                        query_key,
                        outcome=outcome,
                        echo=False,
                        texts=texts,
                        items=items,
                        failed=failed,
                        images=images,
                        used=used,
                    )
                    for query in queries
                ),
                return_exceptions=True,
            )
            usable = 0
            broken = 0
            for entry in results:
                if isinstance(entry, Exception):
                    broken += 1
                    failed.append(f"{tool}：{entry}")
                    continue
                if entry is None:
                    broken += 1
                    continue
                if entry.ok or _looks_like_argument_error(str(entry.error or "")):
                    # 跑通了、或者只是参数写错（工具本身没坏）都不算"工具坏了"
                    usable += 1
                else:
                    broken += 1
            if usable or broken < len(queries):
                return tool
            nxt = self._next_search_tool(tool, candidates)
            if not nxt:
                return tool
            await self._log_event(
                state,
                "skip",
                {
                    "action": definition.id,
                    "note": f"搜索工具「{tool}」用不了，改用「{nxt}」",
                },
                outcome=outcome,
            )
            tool = nxt
        return tool

    @model_operation("search", "搜索结果返回后决定是否继续")
    async def _search_continue(
        self,
        state: WorldState,
        outcome: TickOutcome | None,
        *,
        topic: str,
        asked: list[str],
        items: list[Evidence],
        limit: int = 2,
    ) -> tuple[bool, list[str]]:
        """问主模型：手上这些够了吗？不够就再给几条查询词。

        返回 ``(是否够了, 新的查询词)``。判断不出来（没模型 / 返回不可解析）就当"够了"，
        宁可这次少查一轮，也不要无限查下去。
        """

        if self.llm is None:
            return True, []
        points = []
        for item in items:
            text = "｜".join(
                part
                for part in (
                    item.title,
                    item.published_at,
                    evidence_text(item)[:120],
                )
                if part
            )
            if looks_like_homepage(item.url):
                text += "（这条像首页/栏目页，多半没有正文）"
            points.append(text)
        system_prompt, prompt = self.prompts.build_search_continue_prompt(
            topic=topic,
            asked=asked,
            points=points,
            date_text=self.local_now().strftime("%Y-%m-%d"),
            limit=limit,
        )
        reply = await self._ask_llm(state.session_id, system_prompt, prompt)
        if outcome is not None:
            outcome.llm_calls += 1
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return True, []
        if bool(payload.get("done")):
            return True, []
        raw = payload.get("queries")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return True, []
        result: list[str] = []
        for entry in raw:
            text = " ".join(str(entry).split())[:60]
            if not text:
                continue
            # 补查最容易给回同一件事的几种说法（更新公告 / 更新内容 / 前瞻回顾），
            # 那样等于把同一批结果再搜一遍：和问过的太像就丢掉
            if any(queries_too_similar(text, old) for old in (*asked, *result)):
                continue
            if text not in result:
                result.append(text)
            if len(result) >= max(1, int(limit)):
                break
        return (not result), result

    @model_operation("search", "搜索仍有信息缺口，生成补充查询")
    async def _search_gap_queries(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        items: list[Evidence],
        *,
        topic: str = "",
    ) -> list[str]:
        """证据不够时让辅助模型补几个查询角度（最多两条）。"""

        if self.helper_llm is None or not self._tool_param_allowed(state):
            return []
        topic = topic or self._search_intent(action) or self._search_topic(definition, action)
        system_prompt, prompt = self.prompts.build_search_gap_prompt(
            topic=topic,
            known=[item.title or item.snippet for item in items],
            date_text=self.local_now().strftime("%Y-%m-%d"),
            limit=2,
        )
        reply = await self._ask_judge(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        payload = extract_json_object(reply) if reply else None
        if not isinstance(payload, dict):
            return []
        raw = payload.get("queries")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for entry in raw:
            text = " ".join(str(entry).split())[:60]
            if text and text not in result:
                result.append(text)
        return result[:2]

    async def _read_passage(
        self,
        state: WorldState,
        definition: ActionDef,
        readers: list[str],
        url: str,
        *,
        outcome: TickOutcome | None = None,
        echo: bool = True,
    ) -> str:
        """用阅读工具抓一篇正文（带缓存），失败返回空串。"""

        key = str(url or "").split("?")[0].split("#")[0].rstrip("/").lower()
        cached = self._read_cache.get(key) if key else None
        if cached and (time.monotonic() - cached[0]) < READ_CACHE_SECONDS:
            return cached[1]
        for tool in readers:
            param = self._url_param_name(tool)
            if not param:
                continue
            params = {param: url}
            await self._log_event(
                state,
                "tool_call",
                {
                    "action": definition.id,
                    "tool": tool,
                    "params": dict(params),
                    "note": "读正文",
                },
                outcome=outcome,
                silent=not echo,
            )
            call = await self._call_tool(definition.id, params, state, tool_name=tool)
            await self._log_tool_outcome(
                state, definition, tool, call, outcome=outcome, echo=echo
            )
            if call.ok and call.text:
                # 有的阅读工具返回的是 JSON 外壳（``{"url":…,"content":…}``）：
                # 先取出正文再去掉导航条，最后才截断——顺序反了就会把 JSON 截成半截
                text = _clip_text(
                    clean_passage(unwrap_payload_text(str(call.text))), READ_PASSAGE_CHARS
                )
                if text:
                    if failed_text(text):
                        # 工具有的会"成功返回"一句 extract_failed：那不是正文
                        continue
                    if looks_like_nav(text):
                        # 抓到的是导航/栏目页：当正文用只会把材料带偏，换下一篇
                        await self._log_event(
                            state,
                            "skip",
                            {
                                "action": definition.id,
                                "note": f"这一篇抓回来的是导航页（{url[:60]}），没当正文用",
                            },
                            outcome=outcome,
                        )
                        continue
                    if key:
                        self._read_cache[key] = (time.monotonic(), text)
                        if len(self._read_cache) > 200:
                            self._read_cache.clear()
                    return text
        return ""

    async def _run_search_flow(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        names: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """联网检索形态的动作：多条查询 → 证据 →（可选）读正文 →（可选）补查。"""

        tool = names[0] if names else ""
        candidates = list(names)
        deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
        base = self._action_base_params(definition, action)
        queries = self._explicit_queries(definition, action)
        texts: list[str] = []
        failed: list[str] = []
        images: list[str] = []
        used: list[str] = []
        items: list[Evidence] = []
        asked: list[str] = []
        reads = 0

        action["tool_names"] = list(names)
        action["tool_used"] = []
        if not tool:
            action["tool_name"] = ""
            action["tool_ok"] = False
            action["tool_error"] = "这个动作还没选好可用的搜索工具"
            return

        query_key = self._query_param_name(tool)
        intent = ""
        if not queries:
            # 她自己没写查询词，但参数里已经带了：就用那一条
            given = (
                str(base.get(query_key) or "").strip()
                if query_key
                else self._query_from_params(base)
            )
            if given:
                queries = [given]
        if not queries:
            # 还是没写：先定"要查什么"（她写的 / 配好的 / 让她自己想），
            # 再让辅助模型把这句意图翻成查询词
            intent = await self._search_intent_for(state, definition, action)
            filled, _note = await self.fill_tool_params(
                state,
                definition,
                PlannedAction(type=definition.id, intent=intent),
                tool_name=tool,
                params=base,
                extra_rules=SEARCH_QUERY_RULES,
            )
            if filled:
                base = {**base, **filled}
            generated = str(base.get(query_key) or "").strip() if query_key else ""
            if not generated:
                generated = intent
                if query_key:
                    base[query_key] = generated
            queries = [generated] if generated else []
        elif query_key:
            missing = [
                name for name in self.missing_params_for(tool, base) if name != query_key
            ]
            if missing:
                filled, _note = await self.fill_tool_params(
                    state,
                    definition,
                    PlannedAction(type=definition.id, intent=queries[0]),
                    tool_name=tool,
                    params=base,
                )
                if filled:
                    base = {**base, **filled}

        # 多条查询**并发**发出去：串行的话每条要等上一条搜完（一次 3~4 秒），
        # 既慢又让回显分散成好几行；并发之后它们几乎同时发生，防抖自然合成一行
        tool = await self._search_batch(
            state,
            definition,
            action,
            queries,
            base,
            query_key,
            tool,
            candidates,
            outcome=outcome,
            texts=texts,
            items=items,
            failed=failed,
            images=images,
            used=used,
            asked=asked,
        )

        read_budget, rounds = self._search_budget(definition, action)
        for _ in range(rounds):
            if time.monotonic() > deadline:
                break
            # 够不够、还要不要再来一轮：交给主模型判断
            done, gaps = await self._search_continue(
                state,
                outcome,
                topic=intent or (asked[0] if asked else ""),
                asked=asked,
                items=items,
            )
            if done or not gaps:
                break
            before = len(items)
            # 同一个意思换个说法的查询词（更新内容 / 更新公告 / 前瞻回顾）只当一条，
            # 不然补查一轮只是把同一批结果再搜一遍
            fresh = [
                entry
                for entry in gaps
                if entry not in asked
                and not any(queries_too_similar(entry, old) for old in asked)
            ]
            if not fresh:
                break
            tool = await self._search_batch(
                state,
                definition,
                action,
                fresh,
                base,
                query_key,
                tool,
                candidates,
                outcome=outcome,
                texts=texts,
                items=items,
                failed=failed,
                images=images,
                used=used,
                asked=asked,
            )
            if len(items) <= before:
                # 补了一轮什么都没新拿到：别再往下问了，直接用现有的说
                break

        if read_budget > 0:
            readers = self._reader_tools(definition, state)
            if readers:
                # 只读"看着像某一篇"的链接：搜索经常先给一堆首页/栏目页，
                # 读它们只会浪费调用（回来的还是导航，反而把摘要挤掉）。
                concrete = [
                    item
                    for item in items
                    if item.url and not item.passage and not looks_like_homepage(item.url)
                ]
                # 带摘要的优先读：摘要里已经有线索的那几篇，正文通常也真
                concrete.sort(key=lambda item: 0 if item.snippet else 1)
                # 多备一个候选：抓不到 / 抓到导航页时顶上，不至于白读一篇
                picked = concrete[: read_budget + 1]
                if not concrete:
                    outcome.notes.append(
                        "搜到的都是首页/栏目页，没读正文，直接用搜索摘要"
                    )
                # 读正文同样并发：几篇一起抓，比一篇篇等快得多
                passages = await asyncio.gather(
                    *(
                        self._read_passage(
                            state,
                            definition,
                            readers,
                            item.url,
                            outcome=outcome,
                            echo=False,
                        )
                        for item in picked
                    ),
                    return_exceptions=True,
                )
                for item, passage in zip(picked, passages):
                    if reads >= read_budget:
                        break
                    if isinstance(passage, Exception) or not passage:
                        continue
                    item.passage = str(passage)
                    reads += 1

        items = merge_evidence(items, limit=8)
        digest = await self._digest_evidence(
            state, items, topic=intent or (asked[0] if asked else ""), outcome=outcome
        )
        action["tool_name"] = tool
        action["tool_used"] = used
        action["tool_ok"] = bool(texts or images)
        action["tool_error"] = "；".join(failed)
        action["tool_queries"] = list(asked)
        if images:
            action["tool_images"] = images
        if texts:
            action["tool_result"] = "\n\n".join(texts)
        elif base:
            action.setdefault("params", dict(base))
        if digest:
            # 交给主模型的是"要点+编号"，不是一堆原文；材料本身仍然留在日志里
            action["tool_digest"] = digest
        if items:
            await self._store_search_evidence(
                state, definition, action, items, asked, outcome=outcome
            )
        await self._remember_search(state.session_id, asked, len(items))

    @model_operation("search", "阅读搜索材料并提炼信息")
    async def _digest_evidence(
        self,
        state: WorldState,
        items: list[Evidence],
        *,
        topic: str,
        outcome: TickOutcome | None = None,
    ) -> str:
        """把证据压成"要点 + 编号"；没模型/没额度时返回空串（调用方用原始证据）。"""

        if not items or self.helper_llm is None or not self._tool_param_allowed(state):
            return ""
        # 材料 = 正文（像正文时）或搜索摘要。读到导航页 / 抓取失败时**不能把摘要一起丢**：
        # 以前只喂"精读正文"，于是"搜索明明带回了内容，压出来却说材料里没有"。
        usable = [
            (item, evidence_text(item))
            for item in items
            if evidence_text(item)
        ]
        if len(usable) < 2:
            return ""
        # 材料本来就很少很短时不值得再花一次调用：直接用证据块
        if sum(len(text) for _item, text in usable) < 160:
            return ""
        materials = [
            "｜".join(
                part
                for part in (
                    item.title,
                    item.published_at,
                    text[:400],
                )
                if part
            )
            for item, text in usable[:10]
        ]
        system_prompt, prompt = self.prompts.build_search_digest_prompt(
            topic=topic,
            materials=materials,
            date_text=self.local_now().strftime("%Y-%m-%d"),
        )
        reply = await self._ask_helper(state.session_id, system_prompt, prompt)
        self._count_tool_param(state)
        text = str(reply or "").strip()
        # 把"喂进去的材料"和"压出来的要点"都留一份：材料全是入口页 / 压缩丢内容
        # 这两种情况只靠最终发言分不出来（用户问过"为什么搜出来却说什么都没查到"）。
        await self._log_event(
            state,
            "search_digest",
            {
                "topic": topic,
                "materials": len(materials),
                "material_chars": sum(len(item) for item in materials),
                "preview": _clip_log(" ／ ".join(item[:60] for item in materials[:3])),
                "digest": _clip_log(text),
            },
            outcome=outcome,
        )
        # 压缩说"材料里没有直接答案"时**别拿它当结论**：改用原始证据块，
        # 让主模型自己看搜索摘要（否则"搜到了却说什么都没查到"）。
        if not text or "材料里没有" in text or "没有直接答案" in text:
            return ""
        return _clip_text(text, 1200)

    async def _store_search_evidence(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        items: list[Evidence],
        queries: list[str],
        *,
        outcome: TickOutcome | None = None,
    ) -> None:
        """证据写进 ``action``：编号块给模型看，原文完整留在日志里。"""

        action["tool_evidence"] = [
            {
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "published_at": item.published_at,
                "passage": item.passage,
            }
            for item in items
        ]
        action["tool_evidence_text"] = render_evidence(items)
        sources = sources_of(items)
        action["tool_sources"] = sources
        await self._log_event(
            state,
            "search_sources",
            {
                "action": definition.id,
                "count": len(items),
                "sources": sources,
                "queries": list(queries or []),
            },
            outcome=outcome,
        )

    @staticmethod
    def _search_evidence_text(
        definition: ActionDef | None, action: dict[str, Any]
    ) -> str:
        """检索型动作交给模型的证据块；其它动作返回空串。"""

        if definition is None:
            return ""
        if str(getattr(definition, "tool_flow", "simple")) != "search":
            return ""
        return str(action.get("tool_evidence_text") or "")

    def _satisfy_curiosity(
        self,
        state: WorldState,
        definition: ActionDef,
        action: dict[str, Any],
        outcome: TickOutcome | None = None,
    ) -> float:
        """检索型动作真查到东西之后，好奇心按配置回落一次，返回降了多少。

        为什么要在动作之外再扣一刀：动作自己配的 ``curiosity: -0.06`` 只够抵消一小时的
        自然增长，而她的好奇心是按分钟涨的——指望那一口把好奇心压到阈值以下是做不到的，
        结果就是冷却（45 分钟）一到又去查同一件事，看起来像"疯狂上网"。
        """

        if definition is None:
            return 0.0
        if str(getattr(definition, "tool_flow", "simple")) != "search":
            return 0.0
        amount = float(getattr(definition, "search_satisfy_curiosity", 0.30) or 0.0)
        if amount <= 0:
            return 0.0
        # 什么都没查到（没配检索工具 / 工具全报错）：这一次不算"满足"
        found = str(
            action.get("tool_evidence_text") or action.get("tool_result") or ""
        ).strip()
        if not found:
            return 0.0
        before = float(state.curiosity)
        state.curiosity = max(0.0, min(1.0, before - amount))
        dropped = before - state.curiosity
        if dropped <= 0:
            return 0.0
        if outcome is not None:
            outcome.notes.append(f"查完东西了：好奇心 -{dropped:.2f}")
        return dropped

    async def desire_intimacy(self, state: WorldState, definition: ActionDef | None) -> float:
        """这个动作算多"亲密的肢体接触"（0~1），用来满足欲求。

        三层，从确定到不确定：

        1. 动作自己在编辑器里写了「亲密程度」→ 直接用；
        2. 内置对照表（`DEFAULT_INTIMACY`）里有的 → 用表里的值，不花任何调用；
        3. **用户自己新加的动作**：交给便宜模型判一次（跟"能不能走安抚通道"共用
           同一份判断与缓存，动作改过名字 / 说明才会重判）。
        """

        if definition is None:
            return 0.0
        explicit = getattr(definition, "intimacy", None)
        if explicit is not None:
            try:
                return max(0.0, min(1.0, float(explicit)))
            except (TypeError, ValueError):
                return 0.0
        known = action_intimacy(definition)
        if known > 0:
            return known
        if str(getattr(definition, "target_type", "none") or "none") == "none":
            return 0.0
        await self._ensure_intimacy_traits()
        score = self.intimacy_of(definition)
        if score is None:
            score = await self._classify_intimacy(state, definition)
        try:
            return max(0.0, min(1.0, float(score or 0.0)))
        except (TypeError, ValueError):
            return 0.0

    async def _satisfy_desire_from_action(
        self,
        state: WorldState,
        definition: ActionDef,
        outcome: TickOutcome | None = None,
    ) -> float:
        """亲近一下：抱一抱、摸摸头这种**真的碰到**的动作会满足欲求。

        跟好奇心那条不一样——它不需要"查到东西"才算数：她是真把这一下做到人身上了，
        做完就是做完了。欲求是**跟人有关**的驱力（见 WorldState.desire），
        所以只有对着人的动作才动它，冲着空气做的动作（发呆、看书）没有影响。

        返回降了多少（0 = 这一步不算亲密接触）。
        """

        if definition is None:
            return 0.0
        intimacy = await self.desire_intimacy(state, definition)
        if intimacy <= 0:
            return 0.0
        # 扩展可以按"这一次是什么样的接触"给比例：0 = 一点都不解渴（只升温）
        scale = self.extensions.contact_relief_scale(
            definition, state, state.session_id
        )
        dropped = self.dynamics.satisfy_desire(
            state, intimacy, scale=scale, now=self._now()
        )
        if dropped <= 0:
            return 0.0
        if outcome is not None:
            outcome.notes.append(f"亲近了一下：欲求 -{dropped:.2f}")
        return dropped
