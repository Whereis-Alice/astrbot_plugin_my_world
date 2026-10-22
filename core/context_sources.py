"""Adapt grounded world records into independently selectable context materials."""
from __future__ import annotations

from typing import Any
import json

from .context_selection import (CONTEXT_GUIDANCE, ContextItem, ContextRequest,
                                relevance, section_items, select_context)


def life_items(state: Any, request: ContextRequest) -> list[ContextItem]:
    from .life_matters import (CLOSED, ON_HOLD, current_bookmark, current_stage_notes, pending_review_bookmarks,
                              execution_state, progress_label, stable_interests, stage_record_text, visible)
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
        goal_state = {"completed": "已确认完成", "abandoned": "已放下", "paused": "暂停，目前未执行",
                      "waiting": "等待中，目前未执行", "blocked": "受阻，目前未执行",
                      "expired": "已过期"}.get(status, "尚未完成")
        ready = status not in CLOSED | ON_HOLD
        heading = ("尚未结束的生活事项" if ready else "已经结束的生活事项" if status in CLOSED
                   else "等待接续的生活事项" if status in {"waiting", "blocked"} else "暂时放下的生活事项")
        summary = (f"# {'本会话的约定' if who else heading}\n"
                   f"[{item.get('id') or index}] 目标（{goal_state}）：{title}；状态：{status}；"
                   f"{'当前' if ready else '已记录'}阶段活动：{progress_label(item)}，不是整个目标的完成度；")
        if status in CLOSED:
            summary += "此目标已经结束，旧计划和阶段的后续不再是当前待办；"
        elif status in ON_HOLD:
            summary += f"保留的接续位置（目前未执行）：{item.get('next_step') or '尚未确定'}；"
        else:
            summary += f"下一步（待做）：{item.get('next_step') or '尚待安排'}；"
        summary += (f"阻碍或结束缘由：{item.get('obstacle') or '无'}。\n"
                   "世界内的准备或模拟不代表现实操作成功，约定是否兑现以实际结果为准。")
        topic = [title, str(item.get("motivation") or ""), str(item.get("result") or "")]
        steps = [step for step in item.get("steps", []) if step.get("goal", title) == title]
        bookmark = current_bookmark(item)
        if item.get("needs_review") and status not in CLOSED:
            done = "；".join(str(step.get("text") or "") for step in steps
                            if step.get("status") == "done" and not step.get("stage_noted"))
            review_label = "待复核的活动" if ready else "保留的待复核活动（目前未执行）"
            summary += (f"\n{review_label}：{done or '尚无'}。这里只确认做过活动，尚无该阶段的内容结论；"
                        "步骤结束只登记投入，不能推断其中某一项资料已比对或某个子问题已解决；"
                        "具体所得只依据书签和阶段正文，不能从步骤名称补出未记录的发现或整个目标达成。")
            for last in pending_review_bookmarks(item):
                summary += (f"\n计时结束前最后留下的思路（尚待复核）：{last.get('note') or '未记录新发现'}；"
                            f"未解问题：{last['open_question'] or '未记录'}；"
                            f"{'接续位置（待做）' if ready else '保留的接续位置（目前未执行）'}：{last['next_focus']}。"
                            "计时结束没有自动解决这些疑问。")
                topic.extend(str(last.get(key) or "") for key in ("note", "open_question", "next_focus"))
        elif ready:
            pending = next((step for step in steps if step.get("status") != "done"), None)
            if pending:
                summary += f"\n尚未完成的步骤：{pending.get('text') or ''}"
                question = bookmark.get("open_question") if bookmark else pending.get("open_question")
                if question:
                    summary += f"；仍待核实：{question}"
        topic.extend(str(step.get(key) or "") for step in steps for key in ("text", "focus", "open_question"))
        if item.get("result"):
            summary += f"\n最新结果：{item['result']}"
        for event in reversed(item.get("history", [])):
            if event.get("manual"):
                summary += f"\n最近的人工调整（{event.get('kind') or '调整'}）：{event.get('text') or ''}"
                break
        stages = current_stage_notes(item)
        if stages:
            stage = stages[-1]
            summary += "\n最近阶段记录（世界内模拟，正文与疑问须一起理解）：" + stage_record_text(stage, status=status)
            topic.extend(str(stage.get(key) or "") for key in ("summary", "content", "next_focus"))
        if bookmark:
            label = "中途书签（步骤尚未做完）" if ready else "保留的书签（目前未执行）"
            summary += (f"\n{label}：{bookmark['step']}；上次留下的接续位置：{bookmark['next_focus']}。"
                        "尚未确认这一步的结果，投入和思路都不代表已完成或已核实。")
            if bookmark.get("note"):
                summary += f"\n此前记录的世界内中途思路：{bookmark['note']}"
            if bookmark.get("open_question"):
                summary += f"\n未解问题：{bookmark['open_question']}"
            topic.extend(str(bookmark.get(key) or "") for key in ("note", "next_focus", "open_question"))
        summary += f"\n最新执行状态（上述记录之后的此刻）：{execution_state(state, item)}。"
        detail = summary + (f"\n原先的动机：{item['motivation']}" if item.get("motivation") else "")
        if bookmark:
            detail += (f"\n计时依据（不是成果）：已投入{bookmark['worked_minutes']:g}分钟；"
                       f"预计还需{bookmark['remaining_minutes']:g}分钟，估时可能变化。")
        result.append(ContextItem(f"matter:{item.get('id') or index}", "matter", summary, detail,
            at=float(item.get("updated_at") or item.get("created_at") or 0),
            people=(who,) if who else (), session=str(item.get("session") or "") if who else "",
            continuing=status not in CLOSED | {"paused"},
            priority=.5 if request.task in {"plan", "sleep"} and status not in CLOSED else 0,
            match_text="\n".join(topic)))
    for index, item in enumerate(stable_interests(state, now=request.now)):
        result.append(ContextItem(f"interest:{index}", "experience",
            f"# 近期反复投入的实际活动\n{item['label']}：在 {len(item['days'])} 个不同日期做过，"
            f"累计 {item['minutes']:g} 分钟。投入记录不是新的喜好或性格设定。",
            at=float(item.get("last_at") or 0), match_text=str(item["label"])))
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
            at=float(item.get("at") or 0), session=origin, expires=float(item.get("expires_at") or 0),
            match_text=str(item["content"])))
    return result


def browse_feeling_item(state: Any, request: ContextRequest, browse_id: str) -> ContextItem | None:
    """The just-completed reading is input to its own decision, not a new topic."""
    progress = (state.chat_browsing.get("sessions") or {}).get(request.session) or {}
    if request.task != "participation" or not browse_id or progress.get("browse_id") != browse_id:
        return None
    from .chat_browse import thought_visible
    for index in range(len(state.thoughts) - 1, -1, -1):
        item = state.thoughts[index]
        if (not isinstance(item, dict) or item.get("browse_id") != browse_id
                or item.get("browse_session") != request.session
                or item.get("persona_key") != request.life_persona
                or not thought_visible(item, request.session, request.now)):
            continue
        text = str(item.get("content") or "").strip()
        if not text or len(text) > 120:
            return None
        return ContextItem(f"thought:{index}", "thought",
            "# 本次阅读刚留下的想法（主观感受）\n" + text
            + "\n这是决定是否接话的参考，不能证明亲历、他人动机或新的人物事实。"
              "可以因这个想法继续听，也可以有话想接；结合当前对话再决定，不原样复述这段内心。",
            at=float(item.get("at") or 0), session=request.session,
            expires=float(item.get("expires_at") or 0), priority=10, source="chat_browse",
            match_text=text)
    return None


def life_memory_metadata(source: str, context: Any) -> dict:
    """Only explicit provenance links records; similar wording is insufficient."""
    if source not in {"life_matter", "chat_browse"}:
        return {}
    try:
        value = json.loads(context) if isinstance(context, str) else context
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def memory_context_items(state: Any, request: ContextRequest, memories, *, stamp=None,
                         life_enabled: bool = True) -> list[ContextItem]:
    """Recall a dated fragment together with its current matter, or omit it."""
    matters = {row.key: row for row in life_items(state, request)} if life_enabled else {}
    raw_matters = {str(row.get("id")): row for row in state.life_matters if isinstance(row, dict)}
    result = {}
    for memory in memories:
        if memory.source_scoped and memory.session_id != request.session:
            continue
        people = tuple(memory.participants or memory.related_users)
        if memory.private_matter and (request.task in {"plan", "sleep", "diary"} or not set(people) & set(request.people)):
            continue
        meta = life_memory_metadata(memory.source, memory.context)
        if meta.get("persona_key") and meta["persona_key"] != request.life_persona:
            continue
        label = "# 相关经历（当时的事，按记录理解）\n"
        dependencies = ()
        if memory.source == "chat_browse":
            label = "# 阅读本会话时的感想（主观想法，不是亲历或人物事实）\n"
        elif memory.source == "life_matter":
            if not life_enabled:
                continue
            if request.task == "participation" and not relevance(request.query, memory.content):
                continue  # A fresh life memory is not an invitation to announce it.
            label = "# 当时的生活进展（准备与模拟不等于现实兑现，以所附最新状态为准）\n"
            mid = str(meta.get("matter_id") or "")
            if mid:
                key = "matter:" + mid
                if key not in matters:
                    continue  # Deleted, private or another persona: no orphan fragments.
                result[key] = matters[key]
                dependencies = (key,)
                if meta.get("goal") and meta["goal"] != raw_matters[mid].get("text"):
                    label += f"历史目标：{meta['goal']}；已调整，不证明当前目标已完成。\n"
            else:
                label += "旧记录缺少事项关联，只代表当时片段，不能证明当前状态或目标已完成。\n"
        body = label + ("涉及用户ID：" + "、".join(people) + "\n" if people else "")
        body += memory.render(stamp=stamp(memory.created_at) if stamp else "")
        key = f"memory:{memory.id}"
        result[key] = ContextItem(key, "memory", body, at=memory.created_at, people=people,
            session=memory.session_id if memory.source_scoped else "", dependencies=dependencies,
            source=memory.source, match_text=memory.content)
    return list(result.values())


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
    candidates += memory_context_items(state, request, values.get("memories") or [],
        stamp=lambda at: prompts._memory_stamp(at, now), life_enabled=prompts.world.state_dynamics.own_topic_enabled)
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
