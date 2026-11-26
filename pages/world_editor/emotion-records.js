const statusLabels = {
  active: "仍在意", paused: "已暂停", resolved: "已释怀", expired: "已淡去", archived: "已归档",
};
const openStatuses = new Set(["active", "paused"]);

export function createEmotionRecords({root, el, apiGet, apiPost, confirm, currentSession}) {
  const list = root.querySelector("#emotion-records-list");
  const filter = root.querySelector("#emotion-records-filter");
  const feedback = root.querySelector("#emotion-records-feedback");
  let generation = 0;
  let writingGeneration = -1;
  let items = [];
  let shownSession = "";

  function button(label, callback, className = "ghost small") {
    const control = el("button", className, label);
    control.type = "button";
    control.addEventListener("click", callback);
    return control;
  }

  function clear() {
    generation += 1;
    items = [];
    list.replaceChildren();
    feedback.textContent = "";
  }

  async function reload(session, message = "", focusId = "") {
    clear();
    if (shownSession !== session) filter.value = "all";
    shownSession = session;
    const requested = generation;
    filter.disabled = true;
    if (!session) {
      feedback.textContent = "请先配置要查看的会话。";
      return;
    }
    feedback.textContent = "正在读取未释怀记录…";
    try {
      const result = await apiGet("thoughts/emotions", {session, config_scope: ""});
      if (requested !== generation || currentSession() !== session) return;
      items = result.items || [];
      filter.disabled = false;
      render(message, focusId);
    } catch (error) {
      if (requested !== generation || currentSession() !== session) return;
      feedback.textContent = error.message || "读取未释怀记录失败";
      list.append(button("重新读取", () => reload(session)));
    }
  }

  function dialogueLabel(item) {
    if (item.affects_dialogue) return "可在相关对话中参考";
    if (item.status !== "active") return "已退出对话参考";
    if (item.current_persona === false) return "属于以前的人设";
    if (item.source_enabled === false) return "来源会话已停用";
    if (item.feature_enabled === false) return "功能已关闭";
    return "当前无对话影响";
  }

  function card(item) {
    const row = el("article", "thought-row emotion-record");
    row.dataset.recordId = item.id;
    row.tabIndex = -1;
    const heading = el("div", "thought-head");
    heading.append(
      el("strong", "thought-source", item.kind === "grudge" ? "对人的芥蒂" : "她自己的心事"),
      el("span", "thought-node", statusLabels[item.status] || "已保留"),
      el("span", "thought-time", dialogueLabel(item)),
    );
    const details = [item.source_label || item.session];
    if (item.user_name || item.user_id) details.push(`相关人 ${item.user_name || item.user_id}`);
    if (item.created_text) details.push(`记于 ${item.created_text}`);
    if (item.expires_text && openStatuses.has(item.status)) details.push(`预计淡去 ${item.expires_text}`);
    const error = el("p", "emotion-record-error");
    error.setAttribute("role", "alert");
    row.append(heading, el("p", "emotion-record-body", item.text),
      el("p", "muted emotion-record-meta", details.filter(Boolean).join(" · ")));

    const actions = el("div", "emotion-record-actions");
    const editor = el("form", "emotion-record-editor");
    editor.hidden = true;
    const field = el("label", "field");
    const input = el("textarea");
    input.required = true;
    input.maxLength = 60;
    input.rows = 2;
    const save = el("button", "small", "保存内容");
    save.type = "submit";
    const editorActions = el("div", "emotion-record-actions");
    const edit = button("编辑内容", () => {
      input.value = item.text;
      error.textContent = "";
      editor.hidden = false;
      actions.hidden = true;
      input.focus();
    });
    editorActions.append(save, button("取消编辑", () => {
      editor.hidden = true;
      actions.hidden = false;
      error.textContent = "";
      edit.focus();
    }));
    field.append(el("span", "", "记录内容"), input);
    editor.append(field, editorActions);

    const requestId = generation;
    const session = shownSession;
    const stillSelected = () => generation === requestId && currentSession() === session;
    async function change(operation, message, text) {
      if (writingGeneration === requestId || !stillSelected()) return;
      writingGeneration = requestId;
      const buttons = [...list.querySelectorAll("button")];
      for (const control of buttons) control.disabled = true;
      filter.disabled = true;
      error.textContent = "";
      try {
        if (operation === "delete" && !await confirm({title: "删除未释怀记录？",
          message: "这条记录将被永久删除。", confirmText: "删除"})) return;
        if (!stillSelected()) return;
        await apiPost("thoughts/emotions/update", {session, source_session: item.session, id: item.id,
          revision: item.revision, operation, text, config_scope: ""});
        if (stillSelected()) await reload(session, message, item.id);
      } catch (problem) {
        if (stillSelected()) error.textContent = problem.message || "保存失败，请刷新后重试";
      } finally {
        if (writingGeneration === requestId) writingGeneration = -1;
        if (stillSelected()) filter.disabled = false;
        for (const control of buttons) control.disabled = false;
      }
    }

    editor.addEventListener("submit", event => {
      event.preventDefault();
      const text = input.value.trim();
      if (text) change("edit", "内容已更新。", text);
      else error.textContent = "请填写记录内容。";
    });
    actions.append(edit);
    if (item.status === "active") actions.append(button("暂停参考", () => change("pause", "已暂停对话参考。")));
    if (item.status === "paused" && item.feature_enabled !== false && item.current_persona !== false && item.source_enabled !== false) {
      actions.append(button("恢复参考", () => change("resume", "已恢复对话参考。")));
    }
    if (openStatuses.has(item.status)) {
      actions.append(button("标为已释怀", () => change("resolve", "已标为释怀。")));
    }
    actions.append(button("删除记录", () => change("delete", "记录已删除。"), "ghost small danger"));
    row.append(actions, editor, error);
    return row;
  }

  function render(message = "", focusId = "") {
    const mode = filter.value;
    const rows = items.filter(item => mode === "all" ||
      (mode === "reference" ? item.affects_dialogue : mode === "history" ? !openStatuses.has(item.status) : mode === item.status));
    feedback.textContent = `${message}${rows.length ? `显示 ${rows.length} 条 · 共 ${items.length} 条` :
      items.length ? "这个筛选条件下没有记录。" : "所选会话暂无未释怀记录。"}`;
    list.replaceChildren(...rows.map(card));
    if (focusId) {
      const row = [...list.children].find(element => element.dataset.recordId === focusId);
      (row || filter).focus();
    }
  }

  filter.addEventListener("change", () => render());
  return {reload, clear};
}
