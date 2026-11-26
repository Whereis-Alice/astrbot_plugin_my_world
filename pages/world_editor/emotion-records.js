const statusLabels = {
  active: "仍在意", paused: "已暂停", resolved: "已释怀", expired: "已淡去", archived: "已归档",
};
const openStatuses = new Set(["active", "paused"]);
const recordKey = item => JSON.stringify([item.session, item.id]);

export function createEmotionRecords({root, el, apiGet, apiPost, confirm, formatStamp, currentSession}) {
  const list = root.querySelector("#emotion-records-list");
  const filter = root.querySelector("#emotion-records-filter");
  const feedback = root.querySelector("#emotion-records-feedback");
  const batchToggle = root.querySelector("#emotion-records-batch");
  const selection = root.querySelector("#emotion-records-selection");
  const selectAll = root.querySelector("#emotion-records-select-all");
  const selectedCount = root.querySelector("#emotion-records-selected");
  const deleteSelected = root.querySelector("#emotion-records-delete-selected");
  const batchError = root.querySelector("#emotion-records-batch-error");
  let generation = 0;
  let writingGeneration = -1;
  let items = [];
  let visibleItems = [];
  let shownSession = "";
  let selecting = false;
  const selected = new Set();

  function button(label, callback, className = "ghost small") {
    const control = el("button", className, label);
    control.type = "button";
    control.addEventListener("click", callback);
    return control;
  }

  function clear() {
    generation += 1;
    items = [];
    visibleItems = [];
    selecting = false;
    selected.clear();
    list.replaceChildren();
    feedback.textContent = "";
    batchError.textContent = "";
    syncSelection();
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

  function recordDetails(item) {
    const details = el("details", "emotion-record-detail");
    details.append(el("summary", "", "查看来龙去脉"));
    details.append(el("h4", "", "第一人称叙述"), item.narrative
      ? el("p", "emotion-record-body", item.narrative)
      : el("p", "muted emotion-record-meta", "未保存第一人称叙述。"));
    if (item.evidence?.length) {
      details.append(el("h4", "", "相关对话原话"));
      for (const source of item.evidence) {
        const quote = el("blockquote");
        const stamp = Number(source.at);
        const byline = [source.user_name, Number.isFinite(stamp) && stamp > 0 ? formatStamp(stamp) : "",
          source.recovered ? "按时间关联" : ""];
        const caption = byline.filter(Boolean).join(" · ");
        if (caption) quote.append(el("small", "muted", caption));
        quote.append(el("p", "emotion-record-body", source.text));
        details.append(quote);
      }
    }
    return details;
  }

  function syncSelection() {
    selection.hidden = !selecting;
    batchToggle.textContent = selecting ? "取消批量选择" : "批量选择";
    batchToggle.setAttribute("aria-pressed", String(selecting));
    batchToggle.disabled = !selecting && !visibleItems.length;
    const any = selected.size > 0;
    selectAll.checked = any && selected.size === visibleItems.length;
    selectAll.indeterminate = any && selected.size < visibleItems.length;
    selectAll.disabled = !visibleItems.length;
    selectedCount.textContent = `已选 ${selected.size} 条`;
    deleteSelected.disabled = !any;
    for (const row of list.querySelectorAll(".emotion-record")) {
      const check = row.querySelector(".emotion-record-check input");
      if (check) check.checked = selected.has(row.dataset.recordKey);
      row.dataset.selected = String(Boolean(check?.checked));
    }
  }

  async function write(requestId, endpoint, payload, {error, confirmation, message, focusId}) {
    const session = shownSession;
    const stillSelected = () => generation === requestId && currentSession() === session;
    if (writingGeneration === requestId || !stillSelected()) return;
    writingGeneration = requestId;
    const controls = [...root.querySelectorAll("button, input, textarea, select")].filter(control => !control.disabled);
    for (const control of controls) control.disabled = true;
    error.textContent = "";
    try {
      if (confirmation && !await confirm(confirmation)) return;
      if (!stillSelected()) return;
      const result = await apiPost(endpoint, {session, config_scope: "", ...payload});
      if (stillSelected()) await reload(session, typeof message === "function" ? message(result) : message, focusId);
    } catch (problem) {
      if (stillSelected()) error.textContent = problem.message || "保存失败，请刷新后重试";
    } finally {
      if (writingGeneration === requestId) writingGeneration = -1;
      if (stillSelected()) for (const control of controls) control.disabled = false;
    }
  }

  function card(item) {
    const row = el("article", "thought-row emotion-record");
    row.dataset.recordId = item.id;
    row.dataset.recordKey = recordKey(item);
    row.tabIndex = -1;
    const heading = el("div", "thought-head");
    if (selecting) {
      const label = el("label", "emotion-record-check");
      const check = el("input");
      check.type = "checkbox";
      check.setAttribute("aria-label", `选择「${item.text}」`);
      check.addEventListener("change", () => {
        if (check.checked) selected.add(recordKey(item));
        else selected.delete(recordKey(item));
        batchError.textContent = "";
        syncSelection();
      });
      label.append(check);
      heading.append(label);
    }
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
    const more = recordDetails(item);
    row.append(heading, el("p", "emotion-record-body", item.text),
      el("p", "muted emotion-record-meta", details.filter(Boolean).join(" · ")), more);
    if (selecting) return row;

    const actions = el("div", "emotion-record-actions");
    const editor = el("form", "emotion-record-editor");
    editor.hidden = true;
    const field = el("label", "field");
    const input = el("textarea");
    input.required = true;
    input.maxLength = 60;
    input.rows = 2;
    const narrativeField = el("label", "field");
    const narrative = el("textarea");
    narrative.maxLength = 600;
    narrative.rows = 4;
    narrative.placeholder = "以“我”的口吻写清发生了什么、我在意什么。";
    narrativeField.append(el("span", "", "第一人称叙述"), narrative);
    const save = el("button", "small", "保存内容");
    save.type = "submit";
    const editorActions = el("div", "emotion-record-actions");
    const edit = button("编辑内容", () => {
      input.value = item.text;
      narrative.value = item.narrative || "";
      error.textContent = "";
      more.open = true;
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
    field.append(el("span", "", "简要摘要"), input);
    editor.append(field, narrativeField, editorActions);

    const requestId = generation;
    function change(operation, message, fields = {}) {
      const confirmation = operation === "delete" ? {title: "删除未释怀记录？",
        message: "这条记录将被永久删除。", confirmText: "删除"} : null;
      return write(requestId, "thoughts/emotions/update", {source_session: item.session, id: item.id,
        revision: item.revision, operation, ...fields}, {error, confirmation, message, focusId: recordKey(item)});
    }

    editor.addEventListener("submit", event => {
      event.preventDefault();
      const text = input.value.trim();
      if (text) change("edit", "内容已更新。", {text, narrative: narrative.value.trim()});
      else error.textContent = "请填写简要摘要。";
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
    visibleItems = items.filter(item => mode === "all" ||
      (mode === "reference" ? item.affects_dialogue : mode === "history" ? !openStatuses.has(item.status) : mode === item.status));
    const rows = visibleItems;
    feedback.textContent = `${message}${rows.length ? `显示 ${rows.length} 条 · 共 ${items.length} 条` :
      items.length ? "这个筛选条件下没有记录。" : "所选会话暂无未释怀记录。"}`;
    list.replaceChildren(...rows.map(card));
    syncSelection();
    if (focusId) {
      const row = [...list.children].find(element => element.dataset.recordKey === focusId);
      (row || filter).focus();
    }
  }

  filter.addEventListener("change", () => {
    selected.clear();
    batchError.textContent = "";
    render();
  });
  batchToggle.addEventListener("click", () => {
    selecting = !selecting;
    selected.clear();
    batchError.textContent = "";
    render();
    if (selecting) selectAll.focus();
  });
  selectAll.addEventListener("change", () => {
    selected.clear();
    if (selectAll.checked) for (const item of visibleItems) selected.add(recordKey(item));
    batchError.textContent = "";
    syncSelection();
  });
  deleteSelected.addEventListener("click", () => {
    const chosen = visibleItems.filter(item => selected.has(recordKey(item))).map(item => ({
      source_session: item.session, id: item.id, revision: item.revision,
    }));
    if (!chosen.length) return;
    write(generation, "thoughts/emotions/delete-batch", {items: chosen}, {
      error: batchError,
      confirmation: {title: `删除所选 ${chosen.length} 条未释怀记录？`,
        message: "所选记录将被永久删除。", confirmText: "删除所选"},
      message: result => `已删除 ${result.removed} 条记录。`,
    });
  });
  return {reload, clear};
}
