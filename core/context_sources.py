"""Adapt grounded world records into independently selectable context materials."""
from __future__ import annotations

from typing import Any

from .context_selection import (CONTEXT_GUIDANCE, ContextItem, ContextRequest,
                                section_items, select_context)


def life_items(state: Any, request: ContextRequest) -> list[ContextItem]:
    from .life_matters import CLOSED, current_stage_notes, progress_label, stable_interests, visible
    from .chat_browse import thought_visible
    result = []
    for index, item in enumerate(getattr(state, "life_matters", []) or []):
        if not isinstance(item, dict) or not visible(item, request.session):
            continue
        if item.get("persona_key") and item["persona_key"] != request.life_persona:
            continue
        who = str(item.get("who") or "")
        if who and request.task in {"plan", "sleep", "diary"}:
            continue  # Private promises never become shared autonomous motivations.
        if who and who not in request.people:
            continue
        status = str(item.get("status") or "active")
        title = str(item.get("text") or "")
        summary = (f"# {'本会话的约定' if who else '一直在推进的生活事项'}\n"
                   f"[{item.get('id') or index}] {title}；状态：{status}；{progress_label(item)}；"
                   f"下一步：{item.get('next_step') or '尚待安排'}；阻碍：{item.get('obstacle') or '无'}。\n"
                   "世界内的准备或模拟不代表现实操作成功，约定是否兑现以实际结果为准。")
        detail = summary + (f"\n原先的动机：{item['motivation']}" if item.get("motivation") else "")
        for stage in current_stage_notes(item)[-1:]:
            detail += (f"\n阶段记录（世界内模拟内容）：{stage.get('summary') or ''}；"
                       f"{stage.get('content') or ''}；后续：{stage.get('next_focus') or '尚未确定'}")
        result.append(ContextItem(f"matter:{item.get('id') or index}", "matter", summary, detail,
            at=float(item.get("updated_at") or item.get("created_at") or 0),
            people=(who,) if who else (), session=str(item.get("session") or "") if who else "",
            continuing=status not in CLOSED | {"paused"},
            priority=.5 if request.task in {"plan", "sleep"} and status not in CLOSED else 0))
    for index, item in enumerate(stable_interests(state, now=request.now)):
        result.append(ContextItem(f"interest:{index}", "experience",
            f"# 近期反复投入的实际活动\n{item['label']}：在 {len(item['days'])} 个不同日期做过，"
            f"累计 {item['minutes']:g} 分钟。投入记录不是新的喜好或性格设定。",
            at=float(item.get("last_at") or 0)))
    for index, item in enumerate(getattr(state, "thoughts", []) or []):
        if not isinstance(item, dict) or not item.get("content"):
            continue
        if item.get("persona_key") and item["persona_key"] != request.life_persona:
            continue
        if not thought_visible(item, request.session, request.now, fallback_session=state.session_id):
            continue
        origin = str(item.get("dialogue_session") or item.get("browse_session") or "")
        if request.task in {"plan", "sleep", "diary"} and origin:
            continue
        result.append(ContextItem(f"thought:{index}", "thought",
            ("# 翻消息时留下的感受（主观想法，非亲历或人物事实）\n" if item.get("browse_session")
             else "# 当时留下的内心（主观感受，不是客观事实，也不直接复述）\n") + str(item["content"]),
            at=float(item.get("at") or 0), session=origin, expires=float(item.get("expires_at") or 0)))
    return result


def runtime_context(prompts: Any, state: Any, node: Any, request: ContextRequest,
                    values: dict[str, Any]) -> str:
    """Build only the plugin's dynamic layer; host history/tools stay untouched."""
    now = prompts._now()
    action = state.current_action or {}
    if state.state in {"sleeping", "napping"}:
        activity = "正在睡觉" if state.state == "sleeping" else "正在小睡"
    elif action:
        activity = str(action.get("desc") or action.get("intent") or prompts.action_label(action.get("type")) or "自由活动")
    else:
        from .life import activity_view
        activity = activity_view(state, prompts.world)["label"]
    base = [CONTEXT_GUIDANCE,
            "# 你的状态\n" + prompts._state_block(state, dwell_minutes=prompts._dwell_minutes(state), tick_seconds=prompts.tick_seconds),
            f"# 当前动作：{activity}", f"# 当前位置：{node.name if node else '未知'}"]
    if now is not None:
        base.append(prompts._clock_line(now))
    candidates: list[ContextItem] = []
    if prompts.world.state_dynamics.own_topic_enabled:
        candidates += life_items(state, request)
    # Live relationship boundaries are already selected per actual participant.
    if values.get("profile_text"):
        base.append(values["profile_text"])
    planning = request.task == "plan"
    if node is not None:
        candidates.append(ContextItem("world:reachable", "world", prompts.reach_table(node.id), required=planning))
    in_progress = prompts._in_progress_block(state)
    if in_progress:
        candidates.append(ContextItem("plan:current", "matter", in_progress, continuing=True, required=planning))
    for name, kind in (("weather", "weather"), ("recent_search", "observation"),
                       ("abilities_line", "ability"), ("pending_event", "event"),
                       ("event_journal", "event"), ("life_context", "calendar")):
        candidates += section_items(values.get(name, ""), key=name, kind=kind,
            continuing=name == "pending_event", required=(planning and name == "abilities_line")
            or name in {"weather", "recent_search"})
    for item in values.get("memories") or []:
        # Browse impressions/private promises keep their original disclosure scope.
        if item.source_scoped and item.session_id != request.session:
            continue
        label = "# 相关经历（当时的事，按记录理解）\n"
        if item.source == "chat_browse":
            label = "# 阅读本会话时的感想（主观想法，不是亲历或人物事实）\n"
        elif item.private_matter:
            label = "# 本会话约定的生活进展（准备与模拟不等于现实兑现）\n"
        candidates.append(ContextItem(f"memory:{item.id}", "memory",
            label + ("涉及用户ID：" + "、".join(item.participants or item.related_users) + "\n" if item.participants or item.related_users else "")
            + item.render(stamp=prompts._memory_stamp(item.created_at, now)),
            at=item.created_at, people=tuple(item.participants or item.related_users),
            session=item.session_id if item.source_scoped else "", priority=.3))
    event_lines = []
    for index, item in enumerate(state.recent_events or []):
        if prompts._trivial_event(item):
            continue
        line = prompts._event_line(item)
        if line:
            event_lines.append(line)
            if not planning:
                candidates.append(ContextItem(f"experience:{index}", "experience", "# 最近发生的事\n" + line,
                    at=float(item.get("at") or item.get("created_at") or 0)))
    if planning and event_lines:
        candidates.append(ContextItem("experience:recent", "experience", "# 最近发生的事\n" + "\n".join(event_lines[-8:]),
            at=request.now, priority=.2))
    external = prompts.external_state_block(state)
    if external:
        # The latest externally-owned state remains authoritative and is not rewritten.
        base.append(external)
    inner = prompts._inner_voice(state, request.session, dialogue_only=request.task in {"reply", "participation"})
    if inner:
        candidates.append(ContextItem("inner:current", "thought",
            "# 当时的内心活动（主观想法，仅你可见，不直接复述）\n" + inner))
    for index, topic in enumerate(prompts._due_open_topics(state, request.session)):
        body = prompts.open_topics_layer([topic], who_name="", pronoun="她")
        if body:
            who = str(topic.get("who") or topic.get("user_id") or "")
            candidates.append(ContextItem(f"topic:{index}", "topic", body,
                people=(who,) if who else (), continuing=True,
                session=str(topic.get("session") or "")))
    # A note summarising previous dialogue is a candidate, not an extra instruction.
    note = state.chat_note_text(request.session,
        max_minutes=int(getattr(prompts.world.context, "chat_note_max_minutes", 30)), now=request.now)
    if note:
        candidates.append(ContextItem("chat:note", "topic", "# 刚才的话题备忘\n" + note))
    selection = select_context(request, candidates)
    if selection.rows:
        base.append(selection.render())
    # Preserve chat identities, images and watermarks using the existing renderer.
    watermark = (state.chat_watermarks.get(request.session) or {}) if request.session != state.session_id else {}
    base.extend(prompts.chat_blocks(values.get("recent_chat") or [], "",
        preview=state.chat_preview_text(request.session),
        answered_max=prompts.world.context.chat_answered_lines,
        elsewhere_max=(prompts.world.context.chat_elsewhere_lines
                       if request.task in {"plan", "reply", "expression"} else 0),
        replied_until=float(watermark.get("until", state.chat_replied_until)),
        replied_seq=int(watermark.get("seq", state.chat_replied_seq)),
        recent_replies=list(state.recent_replies or []), mine_names=prompts.bot_name_list(state),
        current_session=request.session, session_labels=values.get("session_labels"),
        image_marks=values.get("chat_images")))
    if values.get("engagement_hint"):
        base.append(values["engagement_hint"])
    if values.get("other_context"):
        base.append("# 其他插件提供的上下文\n" + values["other_context"])
    for note in values.get("extra_notes") or []:
        if note:
            base.append(note)
    if values.get("style_block"):
        base.append(values["style_block"])
    return "\n\n".join(dict.fromkeys(part for part in base if part))
