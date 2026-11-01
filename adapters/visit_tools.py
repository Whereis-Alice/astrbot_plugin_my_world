"""Natural invitations use the same service as the editor, with local actor checks."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from .profile_weaver_tools import FunctionTool, AstrAgentContext, ContextWrapper
from ..core.evidence import direct_text

TOOL_NAMES = ("mw_world_friends", "mw_world_visit", "mw_world_return")
_INVITATION = re.compile(r"串门|拜访|做客|作客|邀请|见个面|见面|去趟|请.{0,20}(来|过来)|去.{0,30}(找|看|玩|坐坐|聚|世界|家)|visit|invite", re.I)
_RETURN = re.compile(r"回家|回来|回去|(?:结束|取消|撤回).{0,10}(串门|拜访|做客|邀请)|return|come back", re.I)
_NEGATIVE = (r"(?:不要|不用|不必|不许|不准|不想|不愿|不打算|没打算|无需|禁止|"
             r"暂时不|暂时别|暂不|先不|先别|不|别|do\s+not|don't|never)\s*"
             r"(?:再|现在|今天|马上|立刻|又)?\s*")
_INVITATION_DENIED = re.compile(_NEGATIVE + r"(?:串门|拜访|做客|作客|邀请|见面|去|请|visit|invite)"
                                r"|(?:取消|撤回|终止|结束).{0,20}(?:串门|邀请|拜访|做客)|cancel.{0,20}(?:visit|invite)", re.I)
_RETURN_DENIED = re.compile(_NEGATIVE + r"(?:回家|回来|回去|结束|取消|撤回|return|come\s+back)", re.I)
_CLAUSE_BREAK = re.compile(r"[\n。！？!?；;，,]")
_INCOMING = re.compile(r"(?:邀请|请|叫|让).{0,40}(?:过来|来)"
                       r"|(?:来|到|去)(?:自己|我(?:们)?|咱(?:们)?)的?(?:家|世界)"
                       r"|invite.{0,40}(?:over|here|to\s+(?:my|our)\s+(?:home|world))", re.I)
_OUTGOING = re.compile(r"(?:去|到|前往)(?!(?:自己|我(?:们)?|咱(?:们)?)的?(?:家|世界))"
                       r".{0,40}(?:世界|家|找|拜访|做客|作客|串门|玩|坐坐)"
                       r"|(?:go|travel).{0,40}(?:visit|home|world)"
                       r"|visit.{0,40}(?:her|their|your)\s+(?:home|world)", re.I)


def _event(context: Any) -> Any:
    return getattr(getattr(context, "context", None), "event", None)


async def _friends(service: Any, scope: str) -> dict[str, Any]:
    """Only the current participating life receives the global friend directory."""
    reader = getattr(service, "global_overview", None) or service.overview
    overview = await reader(scope)
    if overview.get("scope_enabled") is False:
        raise ValueError("这个生活尚未参与串门，请管理员在串门页面勾选后再邀请。")
    return {"peers": overview.get("peers", []), "active": overview.get("active")}


def _scope(plugin: Any, event: Any, *, mutate: bool = False) -> str:
    if event is None or plugin.retired or not plugin.enabled:
        raise ValueError("当前没有可用的真人会话。")
    service = getattr(plugin, "visits", None)
    if service is None or not service.config.enabled:
        raise ValueError("串门尚未开启，请管理员在插件配置中开启。")
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if not plugin.engine.is_enabled(origin):
        raise ValueError("这个会话尚未启用小世界。")
    if mutate:
        if plugin.in_self_initiated_call():
            raise ValueError("此工具需要当前真人明确邀请，后台角色消息不能调用。")
        if service.config.actor != "all" and not plugin._can_admin(event):
            raise ValueError("当前仅管理员可以安排串门。")
        sender = str(event.get_sender_id() or "").strip()
        if not sender:
            raise ValueError("当前没有可确认身份的真人用户，不能安排串门。")
        if sender == str(event.get_self_id() or "").strip():
            raise ValueError("角色自己的消息不能作为邀请授权。")
    return plugin.engine.state_key(origin)


def _evidence(event: Any, evidence: Any, pattern: re.Pattern) -> list[str]:
    text = direct_text(str(event.get_message_str() or ""))
    quoted = str(evidence or "").strip()
    denied = _INVITATION_DENIED if pattern is _INVITATION else _RETURN_DENIED
    # Validate the surrounding current-user clause, so selecting just "去…"
    # from "不要去…" cannot turn a refusal into an invitation. Independent
    # affirmative clauses and reminders such as "别忘了邀请…" remain valid.
    matches = list(re.finditer(re.escape(quoted), text)) if quoted else []
    clauses = []
    breaks = list(_CLAUSE_BREAK.finditer(text))
    for match in matches:
        start = max((item.end() for item in breaks if item.end() <= match.start()), default=0)
        end = min((item.start() for item in breaks if item.start() >= match.end()), default=len(text))
        if not denied.search(text[start:end]):
            clauses.append(text[start:end])
    if not quoted or len(quoted) > 500 or not clauses or not pattern.search(quoted):
        raise ValueError("请以本轮用户明确邀请或归家的原话为依据，不能从旧记录发起。")
    return list(dict.fromkeys(clauses))


def _named_peers(clause: str, peers: list[dict[str, Any]]) -> set[str]:
    """Resolve only public, explicit names; shared names remain ambiguous."""
    references: dict[str, set[str]] = {}
    for peer in peers:
        for field in ("id", "alias", "name", "world"):
            label = str(peer.get(field) or "").strip()
            if len(label) < 2:
                continue
            # ASCII names must not match inside unrelated words, while Chinese
            # naturally attaches a name to verbs and suffixes such as 去…家.
            pattern = re.escape(label)
            if label.isascii():
                pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_])"
            if re.search(pattern, clause, re.I):
                references.setdefault(label.casefold(), set()).add(str(peer["id"]))
    if not references:
        return set()
    groups = list(references.values())
    # A unique world narrows a shared character name; explicitly naming several
    # different friends still lets the model choose one of those friends.
    return set.intersection(*groups) or set.union(*groups)


def _invitation_selection(clauses: list[str], peers: list[dict[str, Any]], peer_id: str, direction: str) -> None:
    if direction not in {"outgoing", "incoming"}:
        raise ValueError("邀请方向必须是去做客或邀请来访。")
    reason = ""
    for clause in clauses:
        incoming, outgoing = bool(_INCOMING.search(clause)), bool(_OUTGOING.search(clause))
        # Conflicting or absent direction words need the model's interpretation.
        explicit = "incoming" if incoming and not outgoing else "outgoing" if outgoing and not incoming else ""
        if explicit and direction != explicit:
            reason = "邀请方向与本轮原话不一致，请按去对方家或邀请来自己家重新选择。"
            continue
        named = _named_peers(clause, peers)
        if named and peer_id not in named:
            reason = "选择的友邻与本轮原话点名的角色或世界不一致，请先查询准确身份。"
            continue
        return
    raise ValueError(reason)


@dataclass
class WorldFriendsTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = TOOL_NAMES[0]
    description: str = "查看当前参与串门的生活可用的友邻角色及状态。全局配对由管理员管理，要邀请时先查准确的 peer_id。"
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        try:
            scope = _scope(self.plugin, _event(context))
            return json.dumps(await _friends(self.plugin.visits, scope), ensure_ascii=False)
        except ValueError as exc:
            return str(exc)


@dataclass
class WorldVisitTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = TOOL_NAMES[1]
    description: str = (
        "仅当本轮真人明确要求串门、拜访或邀请友邻时提出邀请。"
        "只能选择已配对的 peer_id；对方可拒绝或改约，成功提交不代表已经到访。"
        "明确点名友邻或来去方向时，参数必须与本轮原话一致。"
        "不要因转述、引用、历史记录或其他角色的消息自动调用。"
    )
    parameters: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object", "properties": {
            "peer_id": {"type": "string", "description": "友邻查询给出的准确身份。"},
            "reason": {"type": "string", "description": "希望一起做什么，不包含私聊记录。"},
            "direction": {"type": "string", "enum": ["outgoing", "incoming"],
                          "description": "outgoing=去对方家；incoming=邀请对方来自己家。"},
            "evidence": {"type": "string", "description": "从本轮真人消息摘录明确邀请的原话。"},
        }, "required": ["peer_id", "direction", "evidence"],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        try:
            event = _event(context)
            scope = _scope(self.plugin, event, mutate=True)
            clauses = _evidence(event, kwargs.get("evidence"), _INVITATION)
            peer_id = str(kwargs.get("peer_id") or "")
            direction = str(kwargs.get("direction") or "outgoing")
            service = self.plugin.visits
            overview = await _friends(service, scope)
            if _scope(self.plugin, event, mutate=True) != scope or self.plugin.visits is not service:
                raise ValueError("本轮生活或串门服务已变化，请按当前状态重新提出邀请。")
            _invitation_selection(clauses, overview.get("peers", []), peer_id, direction)
            result = await self.plugin.visits.invite(
                scope, peer_id, reason=str(kwargs.get("reason") or "")[:500], direction=direction,
            )
            return "邀请处理结果（以状态为准，不能把待答复说成已到访）：" + json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            return str(exc)


@dataclass
class WorldReturnTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = TOOL_NAMES[2]
    description: str = "本轮真人明确叫她回家、结束串门或取消邀请时使用；保留已确认经过，结束当前串门。"
    parameters: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object", "properties": {
            "evidence": {"type": "string", "description": "本轮要求归家的真人原话。"}},
        "required": ["evidence"],
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        try:
            event = _event(context)
            scope = _scope(self.plugin, event, mutate=True)
            _evidence(event, kwargs.get("evidence"), _RETURN)
            return json.dumps(await self.plugin.visits.return_home(scope), ensure_ascii=False)
        except ValueError as exc:
            return str(exc)


def register_visit_tools(plugin: Any) -> None:
    plugin._owned_visit_tools = {}
    if not plugin.visits.config.enabled:
        return
    tools = [WorldFriendsTool(plugin=plugin), WorldVisitTool(plugin=plugin), WorldReturnTool(plugin=plugin)]
    plugin.context.add_llm_tools(*tools)
    plugin._owned_visit_tools = {tool.name: tool for tool in tools}


def unregister_visit_tools(plugin: Any) -> None:
    getter = getattr(plugin.context, "get_llm_tool_manager", None)
    if not callable(getter):
        return
    manager = getter()
    registered = getattr(manager, "func_list", [])
    for name, owned in list(getattr(plugin, "_owned_visit_tools", {}).items()):
        matches = [tool for tool in registered if getattr(tool, "name", "") == name]
        if len(matches) == 1 and matches[0] is owned:
            plugin.context.unregister_llm_tool(name)
    plugin._owned_visit_tools = {}
