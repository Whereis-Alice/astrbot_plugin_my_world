"""用户画像与心迹画像（跨插件搬运）。"""

from __future__ import annotations

import time
from typing import Any
from .. import profile_weaver as profile_weaver_bridge
from ..contact_knowledge import contact_knowledge
from ..state import WorldState
from .models import MessageContext


class ProfilesMixin:
    # ---------------- 用户画像：提示词那一段 ----------------

    @staticmethod
    def _date_text(stamp: Any) -> str:
        """画像里的日期说成人话（``2026-09-17``）。"""

        try:
            value = float(stamp or 0.0)
        except (TypeError, ValueError):
            return ""
        if value <= 0:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(value))

    def _next_level_name(self, index: int) -> str:
        """比当前档位再高一档叫什么（到顶了返回空串）。"""

        levels = list(getattr(self.world.profile, "levels", None) or [])
        nxt = int(index) + 1
        if 0 <= nxt < len(levels):
            return str(getattr(levels[nxt], "name", "") or "")
        return ""

    def person_payload(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        """把一个人的画像整理成提示词要用的 dict（关系写日期、动作写人话）。"""

        view = self.profiles.view(session_id, user_id)
        if view is None:
            return None
        bonds: list[dict[str, Any]] = []
        for item in self.profiles.bonds(session_id, user_id, statuses=["current"]):
            bonds.append(
                {
                    "type": str(item.get("type") or ""),
                    "status": "current",
                    "since": self._date_text(item.get("since")),
                }
            )
        result = {
            "user_id": view.user_id,
            "name": view.name,
            "affinities": list(view.affinities),
            "bonds": bonds,
            "claims": [
                {
                    "type": str(item.get("type") or ""),
                    "since": self._date_text(item.get("since")),
                    "asserted_by": str(item.get("asserted_by") or ""),
                }
                for item in view.claims
            ],
            "past": [
                {
                    "type": str(item.get("type") or ""),
                    "since": self._date_text(item.get("since")),
                    "until": self._date_text(item.get("until")),
                }
                for item in view.past
            ],
            "affinity": view.affinity,
            "level": {
                "name": view.level.name,
                "index": int(view.level_index or 0),
                "prompt": view.level.prompt,
                "deny": [
                    self.profiles.action_label(item) for item in (view.level.deny or [])
                ],
                # 再熟一点会到哪一档：提示词里用它说清"现在到哪儿为止"
                "next_name": self._next_level_name(int(view.level_index or 0)),
            },
            "negative": bool(getattr(view, "negative", False)),
            "facts": [
                {
                    "kind": str(item.get("kind") or "other"),
                    "text": str(item.get("text") or ""),
                    "evidence": str(item.get("evidence") or ""),
                    "status": str(item.get("status") or "active"),
                }
                for item in view.facts
            ],
            "call_me": view.call_me,
            "call_him": view.call_him,
            "qq_name": view.qq_name,
            "cards": dict(view.cards or {}),
            "digest": view.digest,
            "note": view.note,
            "message_count": view.message_count,
            "days_known": view.days_known,
            # 时间锚点：提示词里要能说"上次跟你说话是多久前"
            #（想念那条线用的是同一对数，但那只在她主动找人时才算）
            "last_talked_at": view.last_talked_at,
            "last_seen_at": view.last_seen_at,
        }
        service = getattr(self, "profile_weaver_service", None)
        if service is not None and service.enabled:
            detail = service.detail_for(session_id, user_id)
            result["facts"] = contact_knowledge(detail, result["facts"])["facts"]
            fields = {item.get("name"): item.get("value") for item in detail.get("fields") or []}
            nickname = next((str(fields[name]).strip() for name in
                             ("昵称", "称呼", "网名", "名字", "别名") if fields.get(name)), "")
            if nickname:
                result["call_me"] = nickname
                result["name"] = nickname
        return result

    def other_people(
        self, state: WorldState, session_id: str, *, exclude_user_id: str = ""
    ) -> list[dict[str, Any]]:
        """聊天记录里最近说过话的其他人（去重，最多按配置给几个）。"""

        limit = max(1, int(self.world.profile.digest_limit))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in reversed(self.chat_window(state, session_id)):
            user_id = str(item.get("user_id") or "")
            if not user_id or item.get("is_self") or user_id in seen:
                continue
            if exclude_user_id and user_id == str(exclude_user_id):
                continue
            seen.add(user_id)
            profile = self.profiles.profile(session_id, user_id)
            if profile is None:
                continue
            payload = dict(profile.get("payload") or {})
            names = [str(entry) for entry in (payload.get("names") or []) if str(entry)]
            bonds = self.profiles.bonds(session_id, user_id, statuses=["current"])
            rows.append(
                {
                    "user_id": user_id,
                    "name": names[-1] if names else str(payload.get("qq_name") or user_id),
                    "label": "、".join(str(bond.get("type") or "") for bond in bonds),
                    "digest": str(profile.get("digest") or ""),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def profile_block(self, state: WorldState, ctx: MessageContext | None = None) -> str:
        """「你在跟谁说话」那一段：当前说话人全文 + 其他人各一行。"""

        if not self.profiles.enabled():
            return ""
        session_id = str(getattr(ctx, "session_id", "") or state.session_id)
        user_id = str(getattr(ctx, "user_id", "") or "")
        person = self.person_payload(session_id, user_id) if user_id else None
        if person is not None:
            person = self._apply_grudge_coldness(state, user_id, person)
        others = self.other_people(state, session_id, exclude_user_id=user_id)
        # Integrated reads remove only copies of fields still present in the
        # structured profile. Older imported experiences without a field survive.
        link = bool(getattr(self.world.profile, "weaver_link", True))
        weaver = getattr(self, "profile_weaver_service", None)
        weaver_owns_profile = bool(weaver is not None and weaver.enabled)
        blocks = [
            self.prompts.profile_block(
                person,
                others,
                limit_others=int(self.world.profile.digest_limit),
                skip_weaver_facts=(
                    (link and bool(getattr(self, "weaver_injecting", False)))
                    and not weaver_owns_profile
                ),
            )
        ]
        if (
            weaver is not None
            and weaver_owns_profile
            and not bool(getattr(self, "weaver_injecting", False))
            and user_id
        ):
            blocks.append(
                weaver.prompt_block(
                    session_id,
                    user_id,
                    str(getattr(ctx, "user_name", "") or ""),
                )
            )
        if weaver is not None and weaver.llm_tools_enabled:
            blocks.append(
                "需要核实偏好、回忆约定或了解联系人时，可以主动调用 mw_profile_lookup。"
                "不传参数查当前说话人，也可按称呼或用户 ID 查询当前会话认识的人；"
                "待确认的经历不可当成定论。"
            )
        return "\n\n".join(block for block in blocks if str(block or "").strip())

    def _apply_grudge_coldness(
        self, state: WorldState, user_id: str, person: dict[str, Any]
    ) -> dict[str, Any]:
        """她记着这个人的账 → 对他的**生效档位降一档**（关系、好感都不动）。

        降档走的是同一套 levels 表，所以"这一档还不能做"会自动跟着变。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return person
        if self.grudge_for(state, user_id) is None:
            return person
        drop = max(0, int(getattr(config, "grudge_level_drop", 1) or 0))
        levels = list(getattr(config, "levels", None) or [])
        if drop <= 0 or not levels:
            return person
        index = max(0, int((person.get("level") or {}).get("index") or 0) - drop)
        level = levels[index]
        cold = dict(person)
        cold["level"] = {
            "name": level.name,
            "index": index,
            "prompt": level.prompt,
            "deny": [self.profiles.action_label(item) for item in (level.deny or [])],
            "next_name": self._next_level_name(index),
            # 编辑器/调试里能看出这一档是被"气着"压下来的
            "cold": True,
        }
        return cold

    # ---------------- 心迹画像（跨插件搬家） ----------------

    def profile_weaver_status(self, data_root: str) -> dict[str, Any]:
        """看看心迹画像装没装、有多少人的画像可以搬过来。"""

        path = profile_weaver_bridge.profiles_path(data_root)
        if path is None:
            return {"installed": False, "path": "", "people": 0, "facts": 0, "modified_at": 0.0}
        items = profile_weaver_bridge.iter_import_items(
            profile_weaver_bridge.load_profiles(path)
        )
        stats = profile_weaver_bridge.summarize(items)
        try:
            modified = float(path.stat().st_mtime)
        except OSError:
            modified = 0.0
        return {
            "installed": True,
            "path": str(path),
            "people": int(stats.get("people") or 0),
            "facts": int(stats.get("facts") or 0),
            "modified_at": modified,
        }

    async def import_profile_weaver(
        self, session_id: str, data_root: str
    ) -> dict[str, Any]:
        """把心迹画像已经记下的东西搬进本插件的通讯录（只追加事实）。

        那个插件继续跑也完全没问题：这里只是**读**它的 profiles.json，
        把字段变成本插件的事实。之后提示词、通讯录、睡眠整理都在本插件这边继续。
        """

        path = profile_weaver_bridge.profiles_path(data_root)
        if path is None:
            return {
                "ok": False,
                "reason": "没找到心迹画像的数据（它还没装，或者还没有任何画像）",
            }
        items = profile_weaver_bridge.iter_import_items(
            profile_weaver_bridge.load_profiles(path)
        )
        if not items:
            return {"ok": False, "reason": "心迹画像的数据还在，但里面没有可搬的画像"}
        items = [item for item in items if not item.get("source_session")
                 or str(item.get("source_session")) == str(session_id)]
        if not items:
            return {"ok": False, "reason": "心迹画像中没有当前会话可导入的记录"}
        result = self.profiles.import_external(session_id, items, source="心迹画像")
        if result.get("ok"):
            self._log(
                "info",
                f"[my_world] 从心迹画像导入：{result.get('people')} 个人、"
                f"{result.get('added')} 条事实（{result.get('merged')} 条已存在）",
            )
        return {**result, "source_people": len(items)}

    PROFILE_WEAVER_SYNC_KV = "profile.weaver_sync_at"

    async def sync_profile_weaver_by_session(self, data_root: str) -> dict[str, Any]:
        """按记录里的会话把数据分别搬进对应的通讯录（自动同步用）。"""

        path = profile_weaver_bridge.profiles_path(data_root)
        if path is None:
            return {"ok": False, "reason": "没找到心迹画像的数据", "people": 0, "added": 0}
        items = profile_weaver_bridge.iter_import_items(
            profile_weaver_bridge.load_profiles(path)
        )
        if not items:
            return {"ok": True, "people": 0, "added": 0, "merged": 0, "skipped": 0}
        sessions = [str(item) for item in self.enabled_session_ids()]
        by_session: dict[str, list[dict[str, Any]]] = {}
        skipped = 0
        for item in items:
            source = str(item.get("source_session") or "")
            if source:
                # 记录指名了某个会话：只搬进本插件也认识的那个；
                # 不认识的（别的群）坚决不往这个群倒。
                target = source if self.session_config(source) is not None else ""
            elif len(sessions) == 1:
                # 没写会话的全局记录：只有唯一一条会话时才敢放进去
                target = sessions[0]
            else:
                target = ""
            if not target:
                # 认不出这条记录属于哪个群：跳过，别把别人家的画像倒进随便一个群
                skipped += 1
                continue
            by_session.setdefault(target, []).append(item)
        people = added = merged = 0
        for session_id, rows in by_session.items():
            result = self.profiles.import_external(session_id, rows, source="心迹画像")
            people += int(result.get("people") or 0)
            added += int(result.get("added") or 0)
            merged += int(result.get("merged") or 0)
        if added:
            self._log(
                "info",
                f"[my_world] 心迹画像自动同步：新增 {added} 条事实"
                f"（{people} 个人，{merged} 条已存在，跳过 {skipped} 条认不出会话的）",
            )
        return {
            "ok": True,
            "people": people,
            "added": added,
            "merged": merged,
            "skipped": skipped,
        }

    async def maybe_sync_profile_weaver(self, data_root: str) -> str:
        """每隔几小时自动同步一次心迹画像（它一直在记新东西）。"""

        profile = getattr(self.world, "profile", None)
        if profile is None or not bool(getattr(profile, "enabled", True)):
            return ""
        if not bool(getattr(profile, "weaver_link", True)):
            return ""
        if not data_root:
            return ""
        try:
            hours = float(getattr(profile, "weaver_sync_hours", 6.0) or 0.0)
        except (TypeError, ValueError):
            hours = 6.0
        if hours <= 0:
            return ""
        now = self._now()
        try:
            last = float(await self.db.call("kv_get", self.PROFILE_WEAVER_SYNC_KV) or 0.0)
        except Exception:
            last = 0.0
        if last and (now - last) < hours * 3600:
            return ""
        if not self.profile_weaver_status(data_root).get("installed"):
            return ""
        # 先记时间：同步失败也等下一个周期，不要在 tick 里反复读盘
        try:
            await self.db.call("kv_set", self.PROFILE_WEAVER_SYNC_KV, float(now))
        except Exception:
            pass
        service = getattr(self, "profile_weaver_service", None)
        if service is not None and service.enabled:
            service._sync_latest_legacy_profiles()
            return ""
        result = await self.sync_profile_weaver_by_session(data_root)
        return "" if not result.get("added") else f"同步到 {result.get('added')} 条画像字段"
