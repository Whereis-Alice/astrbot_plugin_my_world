"""简易人设缓存与声音样例。"""

from __future__ import annotations

from ..model_calls import model_operation

import asyncio
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from ..json_actions import (
    extract_json_object,
    parse_action_payload,
    speakable_messages,
)
from ..models import pronoun_for as pronounce_for
from ..mood import keyword_signal
from ..prompt import lean_prompt
from ..state import WorldState


class PersonaMixin:
    # ---------------- 简易人设（按人格缓存） ----------------

    @staticmethod
    def _persona_brief_key(persona: str) -> str:
        """用主人设的指纹当键：人设一改，键就变了，自然"提示重新生成"。"""

        import hashlib

        body = " ".join(str(persona or "").split())
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        return f"persona_brief.{digest}"

    PERSONA_BRIEF_LATEST = "persona_brief.latest"
    """最近一次保存的简易人设（不按人设指纹分）。

    指纹变了（换人格、换了会话读出的人设不一样）时还能把它拿出来给用户看，
    不然编辑器里那一段直接变空，看着就像"重装插件被重置了"。
    """

    async def persona_brief_state(
        self, session_id: str, *, persona_text: str | None = None
    ) -> dict[str, Any]:
        """读取指定人设的摘要；未指定时读取当前正式人设。"""

        persona = await self._persona_text(session_id) if persona_text is None else str(persona_text)
        key = self._persona_brief_key(persona)
        payload = None
        try:
            payload = await self.db.call("kv_get", key)
        except Exception:
            payload = None
        data = payload if isinstance(payload, dict) else {}
        return {
            "brief": str(data.get("brief") or "").strip(),
            "source": str(data.get("source") or ""),
            "at": float(data.get("at") or 0.0),
            "key": key,
            "persona_chars": len(persona or ""),
            # 指纹对不上时编辑器还能拿这份兜底（并提示"建议重新生成"）
            "latest": await self._latest_persona_brief(),
        }

    async def _latest_persona_brief(self) -> dict[str, Any]:
        try:
            payload = await self.db.call("kv_get", self.PERSONA_BRIEF_LATEST)
        except Exception:
            return {}
        data = payload if isinstance(payload, dict) else {}
        brief = str(data.get("brief") or "").strip()
        if not brief:
            return {}
        return {
            "brief": brief,
            "source": str(data.get("source") or ""),
            "at": float(data.get("at") or 0.0),
            "key": str(data.get("key") or ""),
        }

    async def save_persona_brief(
        self,
        session_id: str,
        brief: str,
        *,
        source: str = "manual",
        persona_text: str | None = None,
    ) -> str:
        """存下这份简易人设。

        按"当时用的那份人设"的指纹存：向导里用草稿生成时也按草稿存，
        等用户把草稿保存成正式人设，指纹就对得上，不会一保存就变"建议重新生成"。
        """

        persona = await self._persona_text(session_id) if persona_text is None else str(persona_text).strip()
        key = self._persona_brief_key(persona)
        text = " ".join(str(brief or "").split())[:800]
        record = {"brief": text, "source": source, "at": self._now()}
        try:
            await self.db.call("kv_set", key, record)
            if text:
                # 最近一份：换了人格 / 换了会话时编辑器还能把它捞出来
                await self.db.call(
                    "kv_set", self.PERSONA_BRIEF_LATEST, {**record, "key": key}
                )
        except Exception as exc:
            self._log("warning", f"简易人设存不进去：{exc}")
        return text

    @model_operation("persona", "管理员请求生成人设简报")
    async def generate_persona_brief(self, session_id: str, *, persona_text: str | None = None, extra_prompt: str = "") -> str:
        """用生成器模型把主人设压成一段摘要。

        ``persona_text``：编辑器里刚改过、还没保存的角色卡。传了就用它——
        向导里刚写完人设就点"生成"，读配置只会拿到旧人设，生成的摘要跟眼前这份对不上。
        """

        persona = await self._persona_text(session_id) if persona_text is None else str(persona_text).strip()
        if not persona.strip():
            return ""
        limit = int(getattr(self.world.events, "persona_brief_chars", 250) or 250)
        system, prompt = self.prompts.build_persona_brief_prompt(
            persona_text=persona, limit=limit
        )
        if extra_prompt.strip():
            prompt += "\n本次补充要求（仍以原人设事实为准）：\n" + extra_prompt.strip()[:2000]
        brief = await self._ask_creator(session_id, system, prompt) or ""
        brief = " ".join(brief.split())[: max(80, limit * 2)]
        if brief:
            await self.save_persona_brief(
                session_id, brief, source="generated", persona_text=persona
            )
        return brief

    async def event_persona_brief(self, session_id: str) -> str:
        """打杂模型用的那份人设：生成过的优先，否则退回主人设前 200 字。"""

        state = await self.persona_brief_state(session_id)
        brief = str(state.get("brief") or "").strip()
        if brief:
            return brief
        persona = await self._persona_text(session_id)
        return " ".join(str(persona or "").split())[:200]

    # ---------------- 声音样例 ----------------

    def persona_samples(self) -> list[dict[str, Any]]:
        """挑中的样例（配置里存的，跟预设走）。"""

        raw = list(getattr(self.world.persona, "samples", None) or [])
        return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("text") or "").strip()]

    @model_operation("evaluation", "管理员运行角色表现测评")
    async def run_eval(
        self,
        session_id: str,
        rounds: list[dict[str, Any]],
        *,
        llm: Any = None,
        concurrency: int = 3,
        user_name: str = "测试者",
        variant: str = "full",
        director_llm: Any = None,
    ) -> dict[str, Any]:
        """使用正式任务上下文选择构建剧本请求，**并发**收集真实回复。

        和真机对话的区别只有一个：**不写任何状态**——水位线、记忆、心情、动作都不动，
        所以可以反复跑、跑给它十遍也不会污染她的世界。它测的正是"这套提示词 + 这个模型"
        在这一轮会说出什么。

        ``variant="lean"`` 时用 :func:`core.prompt.lean_prompt` 剥掉劝导语，
        用来做"提示词该不该减负"的 A/B。

        ``variant="director"`` 时先让便宜模型写一份 3~6 行人话简报，主人格只拿
        「人设 + 简报 + 最近三句 + 最小格式」——用来验证"决策层能不能顶替那一万六千字"。
        """

        model = llm or self.llm
        if model is None:
            return {"ok": False, "reason": "没有可用的模型"}
        if not rounds:
            return {"ok": False, "reason": "没有要问的问题"}
        state = await self.load_state(session_id, cold_start=False)
        node = self.node(state.node_id)
        persona = await self._persona_text(session_id)
        # 画像那一段要有人才渲染：用这个会话最近说话的人，和真机一致
        actives = state.recent_active_users(1)
        who_id = str((actives[0] or {}).get("user_id") or "") if actives else ""
        who_name = str((actives[0] or {}).get("name") or "") if actives else ""
        if who_id:
            profile_text = self.profile_block(
                state,
                SimpleNamespace(session_id=session_id, user_id=who_id, user_name=who_name),
            )
        else:
            profile_text = self.profile_block(state)
        limit = max(1, min(8, int(concurrency or 3)))
        gate = asyncio.Semaphore(limit)
        recent_chat = self.chat_window(state, session_id)

        async def one(
            index: int, item: dict[str, Any], prev_analysis: str = ""
        ) -> dict[str, Any]:
            text = " ".join(str(item.get("user_text") or "").split())
            row = {
                "index": index + 1,
                "scene": str(item.get("scene") or ""),
                "user_text": text,
                "watch": str(item.get("watch") or ""),
                "taboo": str(item.get("taboo") or ""),
                "reply": [],
                "raw": "",
                "error": "",
            }
            if not text:
                row["error"] = "这一轮没问题文本"
                return row
            async with gate:
                try:
                    _cell, say_limit, style_text = self.style_for(state, session_id, remember=False)
                    eval_rows = [*recent_chat, {"name": user_name, "user_id": str(item.get("user_id") or who_id or ""),
                        "origin": session_id, "text": text, "at": self._now()}]
                    request = self.context_request(state, session_id,
                        task="participation" if variant == "participation" else "reply",
                        rows=eval_rows, text=text, focus=str(item.get("user_id") or who_id or ""))
                    request.life_persona = sha256(persona.encode("utf-8")).hexdigest()[:24]
                    # Read-only retrieval: evaluations must not count recalls, log or mutate state.
                    if variant == "participation":
                        system_prompt, user_prompt, quote_targets = self.participation_prompt(
                            state, session_id, persona, rows=eval_rows, request=request)
                    else:
                        system_prompt = self.prompts.build_autonomous_system_prompt(
                            persona_text=persona,
                            state=state,
                            node=node,
                            available_tools=self.available_tools(),
                            memories=self.context_memories(state, request, eval_rows),
                            context_request=request,
                            engagement_hint=self.engagement.hint(state),
                            max_messages=say_limit,
                            recent_chat=self.context_chat(state, request, recent_chat),
                            reasoning=bool(self.world.reasoning_enabled),
                            style_block=style_text,
                            session_directory=self.session_directory(state),
                            current_session=session_id,
                            session_labels=self.session_labels(state),
                            profile_text=self.context_profiles(state, request),
                            samples=self.voice_sample_lines(
                                state, session_id, preview=True
                            ),
                            # 测评也要跟实跑完全一致：该有的字段一个都不能少
                            wants_touch=self.extensions.wants("touch"),
                            json_fields=self.extensions.json_fields(),
                            hidden_actions=self.exhausted_actions(state),
                            **await self.runtime_notes(session_id),
                        )
                        user_prompt = self.prompts.build_reply_user_prompt(
                            user_name=user_name,
                            text=text,
                            is_private=True,
                        )
                    row["context_selection"] = request.trace
                    row["request_chars"] = len(system_prompt) + len(user_prompt)
                    if variant == "lean":
                        system_prompt = lean_prompt(system_prompt)
                    elif variant == "analyst":
                        channel = director_llm or self.judge_llm or self.helper_llm
                        analysis = ""
                        if channel is not None:
                            a_system, a_user = self.prompts.build_analyst_prompt(
                                state=state,
                                node=node,
                                user_text=text,
                                user_name=user_name,
                                recent_chat=recent_chat,
                                profile_text=profile_text,
                                memories=self.context_memories(state, request, eval_rows),
                                other_context=str(
                                    getattr(state, "last_other_context", "") or ""
                                ),
                                open_topics_text=self.prompts.open_topics_layer(
                                    self.prompts._due_open_topics(state, session_id)
                                ),
                                prev_analysis=prev_analysis,
                                pronoun=pronounce_for(self.world.gender),
                            )
                            try:
                                a_reply = await channel.generate(
                                    session_id=session_id,
                                    system_prompt=a_system,
                                    prompt=a_user,
                                    temperature=0.2,
                                )
                                analysis = str(getattr(a_reply, "text", "") or "").strip()
                            except Exception as exc:
                                row["director_error"] = f"{type(exc).__name__}: {exc}"
                        row["analysis"] = analysis
                        extra = self.prompts.analyst_layer(
                            analysis, pronoun=pronounce_for(self.world.gender)
                        )
                        if extra:
                            # 补充而不是替换：原始信息照旧，分析贴在最后（近因位置）
                            system_prompt = f"{system_prompt}\n\n{extra}"
                    elif variant == "director":
                        brief = ""
                        channel = director_llm or self.judge_llm or self.helper_llm
                        if channel is not None:
                            d_system, d_user = self.prompts.build_director_prompt(
                                persona_text=persona,
                                state=state,
                                node=node,
                                recent_chat=recent_chat,
                                profile_text=profile_text,
                                open_topics_text=self.prompts.open_topics_layer(
                                    self.prompts._due_open_topics(state, session_id)
                                ),
                                style_block=style_text,
                                pronoun=pronounce_for(self.world.gender),
                            )
                            try:
                                d_reply = await channel.generate(
                                    session_id=session_id,
                                    system_prompt=d_system,
                                    prompt=d_user,
                                    temperature=0.2,
                                )
                                brief = str(getattr(d_reply, "text", "") or "").strip()
                            except Exception as exc:
                                row["director_error"] = f"{type(exc).__name__}: {exc}"
                        row["brief"] = brief
                        system_prompt = self.prompts.build_director_reply_system(
                            persona_text=persona,
                            brief=brief or "照常回一句。",
                            samples=self.voice_sample_lines(
                                state, session_id, preview=True
                            ),
                            recent_chat=recent_chat,
                            pronoun=pronounce_for(self.world.gender),
                        )
                    row["prompt_chars"] = len(system_prompt)
                    reply = await model.generate(
                        session_id=session_id,
                        system_prompt=system_prompt,
                        prompt=user_prompt,
                    )
                    if reply is None or not getattr(reply, "ok", True):
                        from ..model_call_runtime import safe_error
                        raise RuntimeError(safe_error(getattr(reply, "error", "") or "模型请求未成功"))
                    raw = str(getattr(reply, "text", "") or "")
                    row["raw"] = raw[:2000]
                    # 动作这条线要和真机一致：用**真实白名单**解析，
                    # 这样"她写了个现在做不了的动作"才会被丢掉并记下来
                    allowed_ids = self._parseable_action_ids(state.node_id)
                    parsed = parse_action_payload(
                        raw,
                        available_actions=allowed_ids,
                        valid_nodes=set(self.world.node_map()),
                        max_actions=self.world.limits.max_actions_per_message,
                        max_messages=say_limit,
                        json_fields=self.extensions.json_fields(),
                    )
                    for action in parsed.actions:
                        if action.type == "say" and action.messages:
                            row["reply"].extend(action.messages)
                    if not row["reply"]:
                        spoken = speakable_messages(
                            extract_json_object(raw) or {}
                        )
                        if spoken:
                            row["reply"] = spoken[:say_limit]
                        elif raw.strip() and extract_json_object(raw) is None and not raw.lstrip().startswith(("{", "```")):
                            row["reply"] = [raw.strip()[:200]]
                    if variant == "participation":
                        from ..speech_decision import parse_decision
                        decision = parse_decision(raw, quote_targets)
                        row["reply"] = [decision.say] if decision.say else []
                        row["reply_to"] = decision.reply_to
                        row["decision"] = {"inner": decision.inner, "intent": decision.intent}
                        if decision.error:
                            row["error"] = decision.error
                    if parsed.reasoning:
                        row["reasoning"] = parsed.reasoning
                    row["actions"] = [action.type for action in parsed.actions]
                    row["tools"] = [
                        {"action": action.type, "intent": str(action.intent or "")[:80]}
                        for action in parsed.actions
                        if str(action.intent or "").strip()
                    ]
                    row["actions_dropped"] = [
                        item
                        for item in (parsed.warnings or [])
                        if "动作" in str(item) or "action" in str(item).lower()
                    ][:4]
                    # 提示词里那一轮能用哪些动作，也记一份：方便看"她是不是没用到手边的能力"
                    row["actions_available"] = sorted(
                        str(item) for item in allowed_ids if str(item) != "say"
                    )[:20]
                except Exception as exc:  # 一轮挂了不该让整份考卷作废
                    from ..model_call_runtime import safe_error
                    row["error"] = f"{type(exc).__name__}: {safe_error(exc)}"
            return row

        if variant == "analyst":
            # 分析层要能看到"上一轮说过什么"，所以按顺序跑（其余分支是并发的）
            results = []
            last = ""
            for index, item in enumerate(rounds):
                row = await one(index, item, prev_analysis=last)
                last = str(row.get("analysis") or last)
                results.append(row)
        else:
            results = list(
                await asyncio.gather(
                    *(one(index, item) for index, item in enumerate(rounds))
                )
            )
        return {
            "ok": True,
            "count": len(results),
            "concurrency": limit,
            "results": sorted(results, key=lambda item: item["index"]),
        }

    @model_operation("evaluation", "管理员请求生成测评剧本")
    async def generate_eval_script(self, session_id: str, rounds: int = 20) -> dict[str, Any]:
        """生成测评剧本（**不进提示词**，只给用户拿去比模型）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {"ok": False, "reason": "还没有角色卡：先在「她」那一页填上，或从 AstrBot 导入一份"}
        system, prompt = self.prompts.build_eval_script_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            rounds=rounds,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "模型没给出 JSON", "raw": raw[:600]}
        rows = []
        for item in payload.get("rounds") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("user_text") or "").split())
            if not text:
                continue
            rows.append(
                {
                    "scene": " ".join(str(item.get("scene") or "").split())[:20],
                    "user_text": text[:200],
                    "watch": " ".join(str(item.get("watch") or "").split())[:120],
                    "taboo": " ".join(str(item.get("taboo") or "").split())[:120],
                }
            )
        if not rows:
            return {"ok": False, "reason": "模型没给出可用的剧本", "raw": raw[:600]}
        return {"ok": True, "rounds": rows, "count": len(rows)}

    def _persona_text_now(self) -> str:
        """插件自己那份角色卡正文（优化人设动的是它）。"""

        return str(getattr(self.world.persona, "text", "") or "")

    @model_operation("persona", "管理员请求复盘角色表现")
    async def review_persona(self, session_id: str) -> dict[str, Any]:
        """体检 + 给可逐条采纳的改动（**不落库**：人设是用户最在意的东西）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {
                "ok": False,
                "reason": "还没有插件自己的角色卡：先在「她」那一页填上，或从 AstrBot 导入一份",
            }
        length_hint = f"改完总长控制在原文的 {int(len(persona) * 0.8)}~{int(len(persona) * 1.5)} 字之间"
        system, prompt = self.prompts.build_persona_review_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            length_hint=length_hint,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "模型没给出 JSON", "raw": raw[:600]}

        def clean(value: Any, limit: int = 200) -> str:
            return " ".join(str(value or "").split())[:limit]

        issues = []
        for item in payload.get("issues") or []:
            if not isinstance(item, dict):
                continue
            detail = clean(item.get("detail"))
            if not detail:
                continue
            issues.append(
                {
                    "level": clean(item.get("level"), 20) or "建议改",
                    "kind": clean(item.get("kind"), 20) or "其他",
                    "detail": detail,
                    "quote": clean(item.get("quote"), 120),
                }
            )
        rewrites = []
        for item in payload.get("rewrite") or []:
            if not isinstance(item, dict):
                continue
            before = str(item.get("before") or "").strip()
            after = str(item.get("after") or "").strip()
            # 逐条替换的前提是 before 能在原文里逐字找到——找不到就不让它上桌
            if not before or not after or before not in persona:
                continue
            rewrites.append(
                {
                    "before": before,
                    "after": after,
                    "why": clean(item.get("why"), 20),
                    "found": True,
                }
            )
        adds = []
        for item in payload.get("add") or []:
            if not isinstance(item, dict):
                continue
            text = clean(item.get("text"), 300)
            if not text:
                continue
            adds.append(
                {
                    "field": clean(item.get("field"), 20) or "补充",
                    "text": text,
                    "why": clean(item.get("why"), 20),
                }
            )
        return {
            "ok": True,
            "persona_chars": len(persona),
            "length_hint": length_hint,
            "ok_points": [clean(item) for item in (payload.get("ok") or []) if clean(item)],
            "issues": issues,
            "rewrite": rewrites,
            "add": adds,
            "questions": [clean(item) for item in (payload.get("questions") or []) if clean(item)],
        }

    def apply_persona_changes(
        self, rewrites: list[dict[str, Any]] | None = None, adds: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """把**你点过**的改动写进角色卡（写前留历史；找不到原文的条目跳过并报出来）。"""

        persona = self._persona_text_now()
        if not persona.strip():
            return {"ok": False, "reason": "角色卡是空的，没什么可改"}
        applied: list[str] = []
        skipped: list[str] = []
        text = persona
        for item in rewrites or []:
            if not isinstance(item, dict):
                continue
            before = str(item.get("before") or "")
            after = str(item.get("after") or "")
            if not before or not after:
                continue
            if before not in text:
                skipped.append(f"找不到这段原文：{before[:20]}")
                continue
            text = text.replace(before, after, 1)
            applied.append(f"改写：{before[:16]} → {after[:16]}")
        appended: list[str] = []
        for item in adds or []:
            if not isinstance(item, dict):
                continue
            chunk = " ".join(str(item.get("text") or "").split())
            if not chunk:
                continue
            field = " ".join(str(item.get("field") or "").split()) or "补充"
            appended.append(f"{field}：{chunk}")
        if appended:
            text = text.rstrip() + "\n\n" + "\n".join(f"- {item}" for item in appended)
            applied.extend(f"新增：{item[:24]}" for item in appended)
        if not applied:
            return {"ok": False, "reason": "没有可应用的改动", "skipped": skipped}
        world = self.editable_world()
        persona_raw = dict(world.get("persona") or {})
        persona_raw["text"] = text
        world["persona"] = persona_raw
        warnings = self.save_scoped_settings(world, reason="优化人设")
        self.reload_config()
        return {
            "ok": True,
            "applied": applied,
            "skipped": skipped,
            "warnings": warnings,
            "chars": len(text),
        }

    @model_operation("persona", "管理员请求生成说话样例")
    async def generate_voice_samples(
        self,
        session_id: str,
        scenes: list[str] | None = None,
        *,
        persona_text: str = "",
    ) -> dict[str, Any]:
        """让内容生成模型按场景出候选（**不落库**，交给编辑器挑）。

        生成物一律先进草稿：样例是要长期占提示词位置的，不能被模型自己写进去。

        ``persona_text`` 同 :meth:`generate_persona_brief`：编辑器里还没保存的角色卡优先。
        """

        persona = str(persona_text or "").strip() or await self._persona_text(session_id)
        if not persona.strip():
            return {"ok": False, "reason": "还没有人设：先去「她」那一页填角色卡或从 AstrBot 导入"}
        system, prompt = self.prompts.build_voice_sample_prompt(
            persona_text=persona,
            pronoun=pronounce_for(self.world.gender),
            name=str(getattr(self.world, "bot_name", "") or ""),
            scenes=scenes,
        )
        raw = await self._ask_creator(session_id, system, prompt)
        if not raw:
            return {"ok": False, "reason": "生成模型没有返回内容（检查插件配置里的「内容生成模型」）"}
        payload = extract_json_object(raw)
        scenes_out: list[dict[str, Any]] = []
        known = {key: label for key, label in self.prompts.VOICE_SCENES}
        # 模型可能只回「被撩」这样的短名：用标签开头那一段当别名来认
        alias_to_key = {}
        for key, label in known.items():
            head = label.split("：")[0].split("，")[0].strip()
            if head:
                alias_to_key[head] = key
            alias_to_key[label] = key
        if isinstance(payload, dict):
            for item in payload.get("scenes") or []:
                if not isinstance(item, dict):
                    continue
                scene_label = " ".join(str(item.get("scene") or "").split())
                key = next(
                    (
                        value
                        for alias, value in alias_to_key.items()
                        if alias and (alias in scene_label or scene_label in alias)
                    ),
                    "",
                )
                candidates = []
                for cand in item.get("candidates") or []:
                    if not isinstance(cand, dict):
                        continue
                    text = " ".join(str(cand.get("text") or "").split())
                    if not text:
                        continue
                    candidates.append(
                        {
                            "text": text[:120],
                            "move": " ".join(str(cand.get("move") or "").split())[:20],
                            "why": " ".join(str(cand.get("why") or "").split())[:20],
                        }
                    )
                if candidates:
                    scenes_out.append(
                        {
                            "scene": key,
                            "label": scene_label or known.get(key, ""),
                            "candidates": candidates[:4],
                        }
                    )
        if not scenes_out:
            return {"ok": False, "reason": "模型没给出可用的候选", "raw": raw[:600]}
        return {"ok": True, "scenes": scenes_out}

    TONE_SIGNALS: dict[str, str] = {
        "praise": "positive",
        "attack": "negative",
        "hug": "hug",
        "comfort": "comfort",
        "positive": "positive",
        "negative": "negative",
    }
    """主模型判的口吻 → 声音样例的场景组。``normal`` / 空不映射。"""

    def tone_signal(self, state: WorldState, text: str = "") -> str:
        """这一轮该按哪种场景抽样例。

        先用上一轮主模型判的口吻（它认得反话），没有再用关键词表兜底
        （"你可真行啊"这种反话，词表一定会认错）。
        """

        mapped = self.TONE_SIGNALS.get(str(getattr(state, "last_user_tone", "") or ""))
        if mapped:
            return mapped
        return keyword_signal(text)

    def voice_sample_lines(
        self,
        state: WorldState,
        session_id: str,
        *,
        signal: str = "",
        preview: bool = False,
    ) -> list[dict[str, Any]]:
        """这一轮抽哪几条样例进去：先按场景对，再轮换，避免连着两轮同一组。

        ``signal`` 是**这一轮该按哪种场景说话**（``hug`` / ``negative`` / ``positive``），
        由 ``tone_signal`` 算出来：主模型上一轮判的口吻最准，没有才退回关键词表。
        ``preview=True`` 时只算不记账——预览不该把轮换状态往前推。
        """

        samples = self.persona_samples()
        if not samples:
            return []
        limit = max(1, int(getattr(self.world.persona, "samples_per_turn", 3) or 3))
        prefer: tuple[str, ...] = ()
        if signal == "hug":
            prefer = ("tease",)
        elif signal == "negative":
            prefer = ("snap", "ignored")
        elif signal == "positive":
            prefer = ("comfort", "tease")
        wanted = [item for item in samples if str(item.get("scene") or "") in prefer]
        rest = [item for item in samples if item not in wanted]
        # 轮换：上一轮用过的排到后面，省得连着两轮同一组
        used = set((state.last_voice_samples or []) if isinstance(
            getattr(state, "last_voice_samples", None), list
        ) else [])
        def order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            fresh = [item for item in items if str(item.get("id") or item.get("text") or "") not in used]
            stale = [item for item in items if str(item.get("id") or item.get("text") or "") in used]
            return [*fresh, *stale]

        picked = [*order(wanted), *order(rest)][:limit]
        if not preview:
            state.last_voice_samples = [
                str(item.get("id") or item.get("text") or "") for item in picked
            ]
        return picked
