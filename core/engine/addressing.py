"""判断这句话是不是对她说的。"""

from __future__ import annotations

import hashlib
from typing import Any
from ..json_actions import extract_json_object
from ..models import ActionDef
from ..state import (
    WorldState,
    chat_core_text,
)
from ..state_dynamics import SOOTHE_VALENCE_BELOW
from .constants import (
    DESIRE_TEASE_CEIL,
    DESIRE_TEASE_FLOOR,
    NEGATIVE_TONE_FRESH_HOURS,
    NEGATIVE_TONE_STREAK_SCALE,
    NEGATIVE_VALENCE_LOW_LINE,
    NEGATIVE_VALENCE_ROUND_CAP,
    NEGATIVE_VALENCE_ROUND_WINDOW,
    TONE_INTIMACY_CEIL,
    TONE_INTIMACY_FLOOR,
)
from .models import (
    MessageContext,
    TickOutcome,
)


class AddressingMixin:
    # ---------------- 这句话是不是「对她说的」 ----------------

    def bot_names(self, state: WorldState) -> list[str]:
        """她的各种叫法：全局设置里的 Bot 名称 + 当前 / 原始群名片。"""

        names = [
            str(self.world.bot_name or "").strip(),
            str(state.bot_current_nickname or "").strip(),
            str(state.bot_base_nickname or "").strip(),
        ]
        return [name for name in dict.fromkeys(names) if name]

    PROFILE_HINT_WORDS = (
        "我是", "我叫", "我的名字", "我生日", "我今年", "我喜欢", "我不喜欢", "我不吃",
        "我讨厌", "我爱吃", "我住", "以后叫我", "叫我", "我上班", "我工作", "我养",
        "我老婆", "我老公", "我女朋友", "我男朋友", "我妈", "我爸",
    )
    """他的话里出现这些词，通常是在说"关于他自己"的稳定信息。"""

    def remember_hint(self, text: str, *, ctx: MessageContext | None = None) -> str:
        """他说了关于自己的事时，提醒她顺手把这条写进通讯录。

        「记住」这个动作以前藏在七十多个动作的清单里，说明里还写着"日常寒暄不要用"，
        于是实际上几乎没人用它——档案全靠睡前整理补。这里只在真的像"个人信息"的那一轮
        加一句，平时不啰嗦。
        """

        if not bool(getattr(self.world.profile, "enabled", True)):
            return ""
        body = str(text or "")
        if not body or not any(word and word in body for word in self.PROFILE_HINT_WORDS):
            return ""
        who = str(getattr(ctx, "user_id", "") or "").strip()
        target = (
            f"`user` 填他的号码 `{who}`"
            if who
            else "`user` 填他的号码（上面聊天记录里名字后面括号里那串）"
        )
        return (
            "# 顺手记进通讯录\n"
            "他这句里有关于他自己的信息（称呼、喜好、生日、工作、关系这类以后还用得上的事）。\n"
            "回话之外，**再写一个 `remember` 动作**把它记进通讯录："
            f"{target}，`text` 一句话说清是什么，`evidence` 填**他的原话**（必填），"
            "`kind` 选 喜好 / 厌恶 / 习惯 / 基本信息 / 关系 / 约定。\n"
            "只写进 `memory` 是不够的：那是你自己的回忆，不进他的档案，"
            "下次问起你还是答不出来。\n"
            "如果这句只是玩笑、或者他并没有真的说清楚，就别写。\n"
        )

    def reply_addressing(self, state: WorldState, ctx: MessageContext) -> str:
        """这次回复是「对她说」还是「群里在聊、她去插一句」。

        只有 @ 了她、私聊、叫了她的名字，或者紧接着她自己那句话往下说，
        才算对她说；其余一律按插话处理——否则群里随便一句话都会被她当成
        "有人在指使我"，答非所问还会乱做动作。
        """

        if ctx.is_private or ctx.is_mentioned:
            return "direct"
        if ctx.is_soft_wake:
            # 没人 @ 她，但路由判定这句就是冲她说的：按"对她说"处理，
            # 只是提示词里会写清"没人点名"，别写成"有人 @ 你"。
            return "soft"
        text = str(ctx.text or "")
        for name in self.bot_names(state):
            if name and name in text:
                return "direct"
        # 上下文里最后一条通常就是刚记下的这条消息（先记后回），
        # 把它去掉，看前一条是谁说的：她刚说完话就有人接上，多半还在同一个话题里。
        context = self.chat_context(state)
        prior = list(context)
        if prior and not prior[-1].get("is_self"):
            prior = prior[:-1]
        if prior and prior[-1].get("is_self"):
            return "direct"
        return "interject"

    def speech_density_hint(self, state: WorldState, *, direct: bool = False) -> str:
        """「最近话太密」的提示：把事实摆给模型，让她这轮少说多做。

        ``direct``（有人正在跟她说话）时不劝她闭嘴——只提醒别刷屏。
        不然一群人围着逗她，她反而被自己的"话太密"提示按成一句不说，看着就很呆。
        """

        style = getattr(self.world, "reply_style", None)
        if style is None:
            return ""
        window_minutes = max(1, int(getattr(style, "dense_window_minutes", 10) or 10))
        limit = max(1, int(getattr(style, "dense_max_lines", 4) or 4))
        since = self._now() - window_minutes * 60
        mine = [
            item
            for item in state.recent_chat
            if item.get("is_self") and float(item.get("at") or 0) >= since
        ]
        if len(mine) < limit:
            return ""
        if direct:
            return (
                f"# 说话密度提醒\n"
                f"最近 {window_minutes} 分钟里你已经说了 {len(mine)} 句，偏多。"
                "这一轮**照常接话**，但每句短一点、别连着刷："
                "一句能说清就别拆成一串，该回的还是要回。\n"
            )
        return (
            f"# 说话密度提醒\n"
            f"最近 {window_minutes} 分钟里你已经说了 {len(mine)} 句，密度偏高。"
            "这一轮尽量少说话：能只做动作（发呆、走动、戳一下、想事情…）就别开口；"
            "真有必要回一句时，短一点，或者干脆安静地做自己的事。"
        )

    def chat_is_group(self, session_id: str) -> bool:
        """这个会话是不是群聊。续说那条路径拿不到 ctx，所以从会话配置取。"""

        session = self.session_config(session_id)
        return getattr(session, "type", "group") != "private"

    def _group_is_lively(self, state: WorldState, *, now: float | None = None) -> bool:
        """群里这几分钟是不是聊得很热闹（带动一下她的心潮）。"""

        stamp = self._now() if now is None else float(now)
        window = min(300.0, max(60.0, float(self.world.decider.chat_window_minutes) * 60))
        recent = [
            item
            for item in state.recent_chat
            if not item.get("is_self") and stamp - float(item.get("at") or 0.0) <= window
        ]
        need = max(3, int(self.world.decider.min_messages_to_interject) + 2)
        return len(recent) >= need

    TONE_EVENTS = {
        "praise": "positive_words",
        "hug": "hug_bot",
        "comfort": "hug_bot",
        "attack": "negative_words",
        "positive": "positive_words",
        "negative": "negative_words",
    }
    """口吻 → 情绪脉冲。``normal`` / 空不产生任何脉冲。"""

    def _she_was_hurting(self, state: WorldState, *, user_id: str = "") -> bool:
        """她这会儿是不是真的难受着——决定"被理解"算不算数。

        不设这道门的话，对方随手一句"我懂你"就能加一次心情，那条通道会变成免费回血。
        三个信号随便中一个就算：效价偏低、心里搁着一件事、或者本来就在记他的账。
        """

        if float(state.valence) < 0.45:
            return True
        if str(getattr(state, "heart_knot", "") or "").strip():
            return True
        uid = str(user_id or "")
        if uid:
            try:
                if self.grudge_for(state, uid):
                    return True
            except Exception:
                pass
        return False

    ROUTER_LOOKBACK = 12
    """盖路由判定时往回复查几条留档：一批默认 5 条，宽一点够用，又不会乱认到旧消息。"""

    @staticmethod
    def _router_row_fields(row: dict | None) -> dict[str, Any]:
        """路由判定 → 留档字段（认不出就是空 dict）。

        认两种来源：路由给的那一行原始判定（键是 ``directed`` / ``to`` / ``reason``），
        或者**已经盖过章的留档**（键是 ``router_directed`` / ``router_to`` /
        ``router_reason``，渲染时要原样带出去）。两样都没有——比如一条普通聊天记录——
        就返回空 dict，绝不凭空写一个"不是在跟你说话"。

        只写路由自己的字段，**绝不碰** ``addressing``：那个是主模型回完之后自己标的
        （见 :meth:`_mark_reply_addressing`），事后会无条件覆盖，两边各占各的。
        """

        if not isinstance(row, dict):
            return {}
        if "directed" in row:
            directed = bool(row.get("directed"))
            to_value, reason_value = row.get("to"), row.get("reason")
        elif "router_directed" in row:
            directed = bool(row.get("router_directed"))
            to_value, reason_value = row.get("router_to"), row.get("router_reason")
        else:
            return {}
        fields: dict[str, Any] = {"router_directed": directed}
        to = " ".join(str(to_value or "").split())
        if to:
            fields["router_to"] = to[:40]
        reason = " ".join(str(reason_value or "").split())
        if reason:
            fields["router_reason"] = reason[:160]
        return fields

    @staticmethod
    def _router_row_for(ctx: MessageContext) -> dict[str, Any] | None:
        """ctx 上那份判定里属于**这条消息**的那一条（认不出就是 None）。"""

        view = ctx.router if isinstance(ctx.router, dict) else {}
        rows = view.get("batch") if isinstance(view.get("batch"), list) else []
        key = str(ctx.message_id or "").strip()
        if key:
            for row in rows:
                if (
                    isinstance(row, dict)
                    and str(row.get("message_id") or "").strip() == key
                ):
                    return row
        if len(rows) == 1 and isinstance(rows[0], dict):
            return rows[0]
        return None

    @staticmethod
    def _router_line_matches(row: dict, item: dict) -> bool:
        """老平台没有事件身份时，一条判定算不算这条留档的「谁说的 + 正文」。

        正文可能被留档那一步掐短、或带上注释尾巴，所以不比逐字：一边是另一边的
        开头也算同一条（短的那边至少 4 个字）；发言人给得出就必须对得上。
        """

        def same_line(left: Any, right: Any) -> bool:
            left = chat_core_text(str(left or ""))
            right = chat_core_text(str(right or ""))
            if not left or not right:
                return False
            if left == right:
                return True
            short, long = (left, right) if len(left) <= len(right) else (right, left)
            return len(short) >= 4 and long.startswith(short)

        if not same_line(row.get("text"), item.get("text")):
            return False
        uid = str(item.get("user_id") or "")
        sender = str(row.get("sender_id") or "")
        return not uid or not sender or sender == uid

    def _stamp_router_view(
        self, state: WorldState, ctx: MessageContext, recorded: dict | None = None
    ) -> int:
        """按事件身份把意图路由的事前判定盖到对应留档上；返回盖了几条。

        路由一次给的是**整批**判定，而它重投的只有最后一条：前面几条早在到达时就
        留过档了（本插件旁观钩子的优先级比路由高，先记）。所以这里不新建记录，
        只用 ``message_id`` 认领已有那几条补一段标注——正文根本没进这一轮请求的
        那几条，也就只有这份判定能说明它们冲谁说。

        实在拿不到事件身份（老平台）才退回「谁说的 + 正文」，而且**必须认得唯一**：
        同一句话两个人说过就不猜，宁可不标。
        """

        view = ctx.router if isinstance(ctx.router, dict) else {}
        rows = view.get("batch") if isinstance(view.get("batch"), list) else []
        if not rows:
            return 0
        index: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("message_id") or "").strip()
            if not key:
                continue
            old = index.get(key)
            # 同一条被发了两次（理论上不该有）：以真正放行的那条为准
            if old is None or (row.get("released") and not old.get("released")):
                index[key] = row
        history = [
            item
            for item in list(state.recent_chat or [])[-self.ROUTER_LOOKBACK :]
            if isinstance(item, dict)
        ]
        stamped = 0
        for item in history:
            key = str(item.get("message_id") or "").strip()
            row = index.get(key) if key else None
            if row is None:
                continue
            item.update(self._router_row_fields(row))
            stamped += 1
        if index:
            return stamped
        # 一条身份都对不上：只可能是老平台，按正文认——一条判定只认一条留档
        claimed: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidates = [
                item
                for item in history
                if id(item) not in claimed and self._router_line_matches(row, item)
            ]
            if len(candidates) != 1:
                continue
            candidates[0].update(self._router_row_fields(row))
            claimed.add(id(candidates[0]))
            stamped += 1
        return stamped

    def _mark_reply_addressing(
        self, state: WorldState, ctx: MessageContext, addressing: str
    ) -> str:
        """把"这句其实是在跟别人说话"记在那条消息上。

        结构化信息（@ 名单、引用对象）只能认出"明说"的情况；一句没有 @ 的
        「你倒是说啊」得靠主模型判。它说是别人就信它：下一轮渲染聊天记录时
        会带上「（这句是冲别人说的，不是在问你）」，免得她再揽一次。
        """

        kind = str(addressing or "")
        if kind not in ("me", "others"):
            return ""
        for item in reversed(list(state.recent_chat or [])[-5:]):
            if not isinstance(item, dict):
                continue
            if str(item.get("user_id")) != str(ctx.user_id or ""):
                continue
            item["addressing"] = kind
            return kind
        return ""

    def _comfort_pulse(
        self, state: WorldState, tone: str, *, user_id: str = ""
    ) -> str:
        """安慰类的口吻走"安抚通道"：不占当天的聊天额度（见 dynamics.soothed）。

        - ``hug``：他哄她、贴一贴——她正低落才算；
        - ``comfort``：他还听懂了她说的那几句——要求多一点，效果也强一点。
        """

        now = self._now()
        if str(tone or "") == "comfort":
            if not self._she_was_hurting(state, user_id=user_id):
                return ""
            if self.dynamics.soothed(state, kind="understood", now=now):
                return "understood"
            return ""
        if self.dynamics.soothed(state, kind="soothed", now=now):
            return "soothed"
        return ""

    INTIMACY_CACHE_KEY = "action_intimacy"
    """动作"亲密度"标签的缓存键（存在 kv 表，不写进世界配置 / 预设）。"""

    def _intimacy_traits(self) -> dict[str, Any]:
        cache = getattr(self, "_intimacy_traits_data", None)
        if not isinstance(cache, dict):
            cache = {}
            self._intimacy_traits_data = cache
        return cache

    @staticmethod
    def _action_fingerprint(definition: ActionDef) -> str:
        """动作改过（名字或说明变了）就重新判一次，别拿旧标签糊弄。"""

        raw = f"{definition.name or ''}|{definition.description or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    def intimacy_of(self, definition: ActionDef | None) -> float | None:
        """这个动作有多"亲密"（0~1）：决定它能不能走安抚通道。

        默认动作库靠效果就能认出来（冲着某个人、降孤独 / 抬心潮），不用问模型；
        用户自己加的动作看不出来时返回 ``None``，交给便宜模型判一次（判完记住）。
        """

        if definition is None:
            return 0.0
        if str(getattr(definition, "target_type", "") or "") != "user":
            return 0.0
        complete = getattr(definition, "on_complete", None)
        effects = dict(getattr(complete, "effects", {}) or {})
        warm = any(
            str(effects.get(key) or "").strip().startswith(sign)
            for key, sign in (("loneliness", "-"), ("affect", "+"))
        )
        if warm:
            return 1.0
        cached = self._intimacy_traits().get(str(definition.id))
        if isinstance(cached, dict) and str(cached.get("fp") or "") == self._action_fingerprint(
            definition
        ):
            return float(cached.get("score") or 0.0)
        return None

    async def _ensure_intimacy_traits(self) -> None:
        """把缓存的标签读进来（每个进程一次）。"""

        if getattr(self, "_intimacy_traits_loaded", False):
            return
        self._intimacy_traits_loaded = True
        try:
            value = await self.db.call("kv_get", self.INTIMACY_CACHE_KEY)
        except Exception:
            value = None
        if isinstance(value, dict):
            self._intimacy_traits_data = {
                str(key): dict(item)
                for key, item in value.items()
                if isinstance(item, dict)
            }

    async def _classify_intimacy(
        self, state: WorldState, definition: ActionDef
    ) -> float:
        """让便宜模型判一次"这个动作算不算亲昵接触"，判完缓存（不会每轮都问）。"""

        channel = self.helper_llm or self.llm
        if channel is None:
            return 0.0
        system = (
            "你在给一个角色插件的动作分类。判断这个动作是不是**亲昵的身体接触**"
            "（抱、亲、贴、拉手、摸头、靠肩膀这类，重点是「主动贴近某个人」）。"
            "\n只输出 JSON：{\"intimacy\": 1} 或 {\"intimacy\": 0}"
        )
        prompt = (
            f"动作 id：{definition.id}\n"
            f"动作名字：{definition.name or definition.id}\n"
            f"动作说明：{definition.description or '（没有说明）'}\n"
            f"它的目标：{getattr(definition, 'target_type', '') or '无'}"
        )
        try:
            reply = await channel.generate(
                session_id=state.session_id,
                system_prompt=system,
                prompt=prompt,
                temperature=0.0,
            )
        except Exception as exc:
            self._log("debug", f"判断动作亲密度失败：{exc}")
            return 0.0
        text = getattr(reply, "text", "") if getattr(reply, "ok", False) else ""
        payload = extract_json_object(str(text or ""))
        score = 0.0
        if isinstance(payload, dict):
            try:
                score = 1.0 if float(payload.get("intimacy") or 0) >= 0.5 else 0.0
            except (TypeError, ValueError):
                score = 0.0
        else:
            body = str(text or "").strip()
            if body.startswith("{"):
                return 0.0
            score = 1.0 if ("亲密" in body or "是" == body[:1]) else 0.0
        cache = self._intimacy_traits()
        cache[str(definition.id)] = {"fp": self._action_fingerprint(definition), "score": score}
        try:
            await self.db.call("kv_set", self.INTIMACY_CACHE_KEY, cache)
        except Exception:
            pass
        return score

    async def _maybe_soothe_from_action(
        self,
        state: WorldState,
        definition: ActionDef | None,
        *,
        outcome: TickOutcome | None = None,
    ) -> bool:
        """她主动贴过来这一下，如果她正低落，也算一次安抚（不占聊天额度）。"""

        if definition is None or float(state.valence) >= SOOTHE_VALENCE_BELOW:
            return False
        await self._ensure_intimacy_traits()
        score = self.intimacy_of(definition)
        if score is None:
            score = await self._classify_intimacy(state, definition)
        if float(score or 0.0) < 0.5:
            return False
        if not self.dynamics.soothed(state, kind="soothed", now=self._now()):
            return False
        await self._log_event(
            state,
            "soothed",
            {
                "kind": "soothed",
                "cause": f"她自己贴过来（{definition.name or definition.id}）",
                "valence": round(float(state.valence), 3),
            },
            outcome=outcome,
        )
        return True

    async def _apply_tone_pulse(
        self,
        state: WorldState,
        tone: str,
        *,
        user_id: str = "",
        outcome: TickOutcome | None = None,
    ) -> str:
        """按"对方这一轮的口吻"给一次情绪脉冲（心潮 / 孤独感的来源之一）。

        主路径是主模型在 JSON 里写的 ``tone``；只有它没写时才用关键词表兜底——
        表只认字面，遇到反话就会判错（"你可真行啊"当年被当成夸奖）。

        安慰类（``hug`` / ``comfort``）还额外走一次"安抚通道"：她低潮时那一下不占
        当天的聊天额度，见 ``_comfort_pulse``。
        """

        key = str(tone or "").strip().lower()
        event = self.TONE_EVENTS.get(key)
        if not event:
            return ""
        magnitude = self._tone_magnitude(state, event, user_id)
        # 扩展可以给这一下打折（例如"正演着那一场戏，他说的重话是玩闹"）：
        # 没扩展表态就是 1.0，一切照旧。
        scale = self.extensions.tone_scale(state, key)
        if event == "negative_words":
            # 连着被伤递减：第一次最疼，之后麻木一点（不然每轮都疼到同一个程度）
            streak_scale = self._negative_streak_scale(state)
            scale = scale * streak_scale if scale != 1.0 else streak_scale
        if scale != 1.0:
            magnitude = round(magnitude * scale, 4)
            if outcome is not None:
                outcome.notes.append(f"这一轮的口吻脉冲按 {scale:g} 倍走")
        before_valence = float(state.valence)
        self.dynamics.apply_event(state, event, magnitude=magnitude, now=self._now())
        if event == "negative_words":
            # 一句话最多让她掉这么多（含基线连带）：不然特别亲近的人一句重话
            # 就能把她打进冷脸，还挂半天。
            self._cap_negative_valence(state, before_valence)
        if key in ("hug", "comfort"):
            kind = self._comfort_pulse(state, key, user_id=user_id)
            if kind:
                await self._log_event(
                    state,
                    "soothed",
                    {
                        "kind": kind,
                        "cause": (
                            "他听懂了你那几句" if kind == "understood" else "被安抚了一会儿"
                        ),
                        "valence": round(float(state.valence), 3),
                    },
                    outcome=outcome,
                )
            # 被他撩了一下（这一轮只是嘴上/氛围上，不是真的碰到）：想被碰一碰的劲头跳一点。
            # 同一句口吻连着来不重复算，越亲近的人越撩得动她。
            if key != str(getattr(state, "last_user_tone", "") or ""):
                gained = self.dynamics.tease_desire(
                    state, scale=self._desire_tease_scale(state, user_id)
                )
                if gained > 0 and outcome is not None:
                    outcome.notes.append(f"被撩了一下：欲求 +{gained:.2f}")
        return event

    def _desire_tease_scale(self, state: WorldState, user_id: str) -> float:
        """这一下撩动她多少：按关系档位从 0.3 涨到 1.6（跟"被怼多疼"同一套档位）。"""

        uid = str(user_id or "")
        if not uid or not self.profiles.enabled():
            return 1.0
        try:
            view = self.profiles.view(self.profile_session(state, uid), uid)
        except Exception:
            return 1.0
        if view is None:
            return DESIRE_TEASE_FLOOR
        levels = list(getattr(self.world.profile, "levels", None) or [])
        if len(levels) <= 1:
            return 1.0
        index = max(0, min(int(getattr(view, "level_index", 0) or 0), len(levels) - 1))
        ratio = index / (len(levels) - 1)
        return round(DESIRE_TEASE_FLOOR + (DESIRE_TEASE_CEIL - DESIRE_TEASE_FLOOR) * ratio, 3)

    def _tone_magnitude(self, state: WorldState, event: str, user_id: str) -> float:
        """这一句话在她这儿有多重：**越亲近的人，伤她越深**。

        路人怼她一句几乎不往心里去（0.2 倍），特别的人说一句顶得多一些（1.15 倍）。
        只对负面口吻（``negative_words``）加权：正面那几条本来就压得很小、还带"连着被哄会麻木"，
        再按亲密度放大反而会让熟人一句夸奖把她顶满。

        "连着被伤会递减"不在这里算，见 :meth:`_negative_streak_scale`：
        这里只回答"这句话按亲密度该有多重"。
        """

        if event != "negative_words":
            return 1.0
        uid = str(user_id or "")
        if not uid or not self.profiles.enabled():
            return 1.0
        try:
            view = self.profiles.view(self.profile_session(state, uid), uid)
        except Exception:
            return 1.0
        if view is None:
            return TONE_INTIMACY_FLOOR
        levels = list(getattr(self.world.profile, "levels", None) or [])
        if len(levels) <= 1:
            return 1.0
        index = max(0, min(int(getattr(view, "level_index", 0) or 0), len(levels) - 1))
        # 归一化到 0~1 再上一条幂曲线：低档更平（路人几乎没感觉），高档更陡
        ratio = index / (len(levels) - 1)
        return round(
            TONE_INTIMACY_FLOOR + (TONE_INTIMACY_CEIL - TONE_INTIMACY_FLOOR) * ratio**1.6,
            3,
        )

    def _negative_streak_scale(self, state: WorldState) -> float:
        """连着被伤时这一笔打几折：第一次最疼，之后递减（隔一天重新算）。

        一直戳同一处，人不该每次都疼到同一个程度。
        """

        now = self._now()
        last = float(getattr(state, "negative_tone_at", 0.0) or 0.0)
        streak = int(getattr(state, "negative_tone_streak", 0) or 0)
        if not last or (now - last) > NEGATIVE_TONE_FRESH_HOURS * 3600.0:
            streak = 0
        state.negative_tone_streak = streak + 1
        state.negative_tone_at = now
        return NEGATIVE_TONE_STREAK_SCALE[
            min(streak, len(NEGATIVE_TONE_STREAK_SCALE) - 1)
        ]

    def _cap_negative_valence(
        self, state: WorldState, before: float, *, now: float | None = None
    ) -> None:
        """一轮里负面情绪最多把效价压低 ``NEGATIVE_VALENCE_ROUND_CAP``。

        这一笔可能来自口吻脉冲、也可能来自主模型自己写的 ``valence_delta``；两笔
        还会顺手把孤独 / 心潮推上去，从而**带低效价基线**——所以这里量的是
        "看得见的效价掉了多少"，超出的部分补回 offset，等于这一句没那么伤。
        已经低于 ``NEGATIVE_VALENCE_LOW_LINE``（正在冷脸）时一点不再往下压。
        """

        now = float(now if now is not None else self._now())
        after = float(state.valence)
        dropped = float(before) - after
        if dropped <= 0:
            return
        last = float(getattr(state, "negative_valence_at", 0.0) or 0.0)
        used = (
            float(getattr(state, "negative_valence_sum", 0.0) or 0.0)
            if last and (now - last) <= NEGATIVE_VALENCE_ROUND_WINDOW
            else 0.0
        )
        budget = 0.0 if float(before) <= NEGATIVE_VALENCE_LOW_LINE else NEGATIVE_VALENCE_ROUND_CAP
        allowed = max(0.0, budget - used)
        if dropped > allowed:
            excess = dropped - allowed
            state.valence_offset = max(
                -0.5, min(0.5, float(state.valence_offset or 0.0) + excess)
            )
            # 补回 offset 之后要把"看得见的效价"重算一遍，不然它还是掉下去的那个值
            try:
                self.dynamics.refresh(state, now=now)
            except Exception:
                pass
            dropped = allowed
        state.negative_valence_at = now
        state.negative_valence_sum = used + dropped

    def note_open_topic(self, state: WorldState, ctx: Any, text: str) -> None:
        """记下"他没说完的事"。空字符串 = 这轮没有，什么都不做。

        同一件事反复说只刷新时间；同一个人身上最多留两件——攒多了她会变成回访客服。
        """

        body = " ".join(str(text or "").split())[:80]
        if not body:
            return
        config = self.world.context
        delay = max(5, int(getattr(config, "open_topic_delay_minutes", 60) or 60)) * 60
        per_person = max(1, int(getattr(config, "open_topic_per_person", 2) or 2))
        now = self._now()
        who = str(getattr(ctx, "user_id", "") or "")
        session = str(getattr(ctx, "session_id", "") or state.session_id)
        items = [dict(item) for item in (state.open_topics or []) if isinstance(item, dict)]
        for item in items:
            existing = str(item.get("text") or "")
            if str(item.get("who") or "") != who:
                continue
            if existing == body or (existing and (existing in body or body in existing)):
                item["text"] = body
                item["at"] = now
                item["next_ask_at"] = now + delay
                item["asked"] = 0
                state.open_topics = items
                return
        items.append(
            {
                "text": body,
                "who": who,
                "who_name": str(getattr(ctx, "user_name", "") or ""),
                "session": session,
                "at": now,
                "next_ask_at": now + delay,
                "asked": 0,
            }
        )
        mine = [item for item in items if str(item.get("who") or "") == who]
        if len(mine) > per_person:
            keep_ids = {id(item) for item in mine[-per_person:]}
            items = [item for item in items if item not in mine or id(item) in keep_ids]
        state.open_topics = items[-8:]
        self._log("debug", f"记下一件没聊完的事：{body}")

    def mark_open_topics_asked(self, state: WorldState, ctx: Any) -> None:
        """这一轮跟他说过话：把到点的那几件标成"已经问过"，并按次数退避。"""

        if not state.open_topics:
            return
        config = self.world.context
        delay = max(5, int(getattr(config, "open_topic_delay_minutes", 60) or 60)) * 60
        max_asks = max(1, int(getattr(config, "open_topic_max_asks", 2) or 2))
        keep_days = max(1, int(getattr(config, "open_topic_days", 7) or 7))
        now = self._now()
        who = str(getattr(ctx, "user_id", "") or "")
        session = str(getattr(ctx, "session_id", "") or state.session_id)
        kept: list[dict[str, Any]] = []
        for item in state.open_topics:
            if not isinstance(item, dict):
                continue
            topic_session = str(item.get("session") or "")
            due = float(item.get("next_ask_at") or 0.0)
            aged = now - float(item.get("at") or 0.0) >= keep_days * 86400
            same_person = str(item.get("who") or "") == who
            same_place = not topic_session or topic_session == session
            if aged:
                continue
            if same_person and same_place and (not due or due <= now):
                asked = int(item.get("asked") or 0) + 1
                if asked >= max_asks:
                    continue
                item["asked"] = asked
                item["next_ask_at"] = now + delay * (asked + 1)
            kept.append(item)
        state.open_topics = kept

    def _apply_valence_delta(self, state: WorldState, raw: float) -> None:
        """把模型给的 -1~1 心情变化落到效价上（单轮上限 + 每天额度）。

        日常聊天是最高频的事，所以这里卡得比"她真经历了一件事"更紧：
        单轮最多 ``chat_valence_cap``（模型给 1.0 也只有这么多），
        一天的正向累计最多 ``chat_valence_daily_cap``（额度在 dynamics 那层统一扣，
        关键词兜底的正面脉冲也走同一份额度）。
        没有这两道闸，随便撩两句就能把效价顶满，那"100 = 极端"就不成立了，
        表达格子也会永远落在同一格（看起来就是死板）。
        """

        try:
            value = float(raw)
        except (TypeError, ValueError):
            return
        try:
            per_turn = float(
                getattr(self.world.state_dynamics, "chat_valence_cap", 0.05) or 0.0
            )
        except (TypeError, ValueError):
            per_turn = 0.05
        delta = max(-1.0, min(1.0, value)) * per_turn
        if not delta:
            return
        # 这一轮聊下来的感受：记成"心情来源"，她下一轮就知道自己为什么这样
        cause = "刚才这几句聊得开心" if delta > 0 else "刚才这几句聊得不舒服"
        before_valence = float(state.valence)
        self.dynamics.apply_event_delta(
            state, "valence", delta, now=self._now(), cause=cause, chat=True
        )
        if delta < 0:
            # 和口吻脉冲共用同一份额度：一轮里负面最多压这么多
            self._cap_negative_valence(state, before_valence)
