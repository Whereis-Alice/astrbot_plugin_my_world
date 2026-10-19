/** The model ledger: filter locally selected scopes, read provider-reported usage. */
export const CALL_PURPOSES = {
  chat_browse: "翻消息时的感想",
  interaction_style: "相处习惯归纳",
  life_matter: "生活事项",
  life_rerank: "生活记忆重排",
  embedding: "语义记忆检索",
  chat: "对话回复", plan: "自主决策", tick: "生活推进", event: "事件推进与结算", consolidate: "睡眠整理",
  context: "上下文管理", memory: "记忆处理", helper: "参数补全", judge: "辅助判断", creation: "内容生成",
  persona: "角色与样例", evaluation: "角色测评", vision: "图片转述", vision_relation: "图片话题分析",
  forward: "转发摘要", profile: "画像提取", reflection: "对话复盘", followup: "动作后续回应",
  schedule: "日程执行", search: "搜索与阅读", weather: "天气整理", action: "动作文字", extension: "扩展调用",
  main: "主模型调用", creator: "内容生成", sleep: "睡眠决策", other: "其他调用",
};
const STATUS = {success: "成功", error: "失败", cancelled: "已取消"};
const number = value => value == null ? "—" : Number(value).toLocaleString("zh-CN");
const stamp = value => new Date(Number(value) * 1000).toLocaleString("zh-CN", {hour12: false});
const duration = value => value < 1000 ? `${number(value)} ms` : `${(Number(value) / 1000).toFixed(1)} s`;

export function createModelCallsPage({ root, apiGet, el, openDecisions }) {
  const $ = id => root.querySelector(`#${id}`);
  let page = 1, revision = 0, searchTimer, timer, data;
  const list = $("calls-list");

  function updateSelect(id, values, label) {
    const control = $(id), value = control.value;
    control.replaceChildren(new Option(label, ""));
    for (const item of values) control.add(new Option(id === "calls-purpose" ? CALL_PURPOSES[item] || item
      : id === "calls-session" ? data.session_labels?.[item] || item : item, item));
    if (values.includes(value)) control.value = value;
  }

  function metric(label, value, note, className = "") {
    const card = el("div", `calls-metric ${className}`);
    card.append(el("span", "people-eyebrow", label), el("strong", "", value), el("small", "", note));
    return card;
  }

  function renderSummary() {
    const summary = data.summary || {};
    $("calls-metrics").replaceChildren(
      metric("请求记录", number(summary.calls || 0), `成功 ${number((summary.calls || 0) - (summary.errors || 0) - (summary.cancelled || 0))} · 取消 ${number(summary.cancelled || 0)}`),
      metric("已知 token", number(summary.reported ? summary.total : null), `${number(summary.reported || 0)} / ${number(summary.calls || 0)} 条提供用量`, "calls-metric-accent"),
      metric("已知输入 / 输出", `${number(summary.input_reported ? summary.input : null)} / ${number(summary.output_reported ? summary.output : null)}`, `缓存输入 ${number(summary.cached_reported ? summary.cached : null)} · 已包含在输入中`),
      metric("失败请求", number(summary.errors || 0), `平均耗时 ${duration(summary.duration_ms || 0)}`, summary.errors ? "calls-metric-error" : ""));
    $("calls-total").textContent = `${number(summary.calls || 0)} 条记录`;
    $("calls-coverage").textContent = `当前筛选 · ${data.hours === 24 ? "最近 24 小时" : `最近 ${data.hours / 24} 天`} · ${number(summary.final_only || 0)} 条仅含最终响应 · 用量缺失 ${number((summary.calls || 0) - (summary.reported || 0))} 条`;
    const filters = ["calls-session", "calls-purpose", "calls-provider", "calls-status"].filter(id => $(id).value).length;
    $("calls-filter-count").textContent = filters ? String(filters) : "";
    const chart = $("calls-chart");
    chart.replaceChildren();
    const hours = data.hours || 24;
    // Long periods use daily buckets, while the last day keeps hourly detail.
    const step = hours > 24 ? 86400 : 3600, count = (hours > 24 ? hours / 24 : 24) + 1;
    const now = Date.now() / 1000, end = Math.floor(now / step) * step;
    const buckets = Array.from({length: count}, (_, i) => ({at: end - (count - 1 - i) * step, calls: 0, tokens: 0}));
    for (const item of data.series || []) {
      const bucket = buckets.find(b => Math.floor(item.at / step) * step === b.at);
      if (bucket) { bucket.calls += item.calls; bucket.tokens += item.tokens; }
    }
    const maximum = Math.max(1, ...buckets.map(item => item.tokens));
    for (const bucket of buckets) {
      const bar = el("span", `calls-bar ${bucket.calls ? "has-calls" : ""}`);
      bar.style.setProperty("--bar-height", `${Math.max(bucket.calls ? 5 : 2, bucket.tokens / maximum * 100)}%`);
      bar.title = `${stamp(bucket.at)} · ${number(bucket.calls)} 条记录 · ${number(bucket.tokens)} 已知 token`;
      bar.setAttribute("aria-label", bar.title);
      chart.append(bar);
    }
    $("calls-chart-start").textContent = new Date((now - hours * 3600) * 1000).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
    $("calls-chart-end").textContent = "现在";
  }

  function field(box, label, value) {
    const item = el("div", "calls-field");
    item.append(el("dt", "", label), el("dd", "", value == null || value === "" ? "未提供" : String(value)));
    box.append(item);
  }

  function renderRows() {
    list.replaceChildren();
    if (!data.items?.length) {
      const empty = el("div", "calls-empty");
      empty.append(el("span", "", "◎"), el("strong", "", "这里还没有调用记录"),
        el("p", "", "插件下一次调用模型后会显示在这里。也可以调整时间或筛选条件。"));
      list.append(empty);
    }
    for (const item of data.items || []) {
      const row = el("details", "calls-row"); row.dataset.id = item.id;
      const head = el("summary", "calls-row-head");
      const identity = el("span", "calls-identity");
      const title = el("span", "calls-row-title");
      title.append(el("strong", "", CALL_PURPOSES[item.purpose] || item.purpose || "其他调用"));
      if (item.attempt > 1) title.append(el("span", "calls-retry", `重试 ${item.attempt - 1}`));
      if (item.coverage && item.coverage !== "direct") title.append(el("span", "calls-final-badge", "仅最终响应"));
      identity.append(title, el("span", "calls-reason", item.retry_reason || item.reason || "插件发起模型请求"),
        el("small", "calls-meta", `${stamp(item.started_at)} · ${item.session_label || item.session_id || "编辑器 / 后台任务"}`));
      const model = el("span", "calls-model");
      model.append(el("strong", "", item.model || "模型名未提供"), el("small", "", item.provider_id || "Provider 未提供"));
      const tokens = el("span", "calls-tokens");
      tokens.append(el("strong", "", number(item.usage?.total)), el("small", "", item.usage?.available ? "token" : "用量未提供"));
      const state = el("span", `calls-result is-${item.status}`);
      state.append(el("b", "", STATUS[item.status] || item.status), el("small", "", duration(item.duration_ms || 0)));
      head.append(identity, model, tokens, state, el("span", "calls-expand", "+"));
      const content = el("div", "calls-detail");
      const path = el("div", "calls-trigger-path");
      path.append(el("span", "people-eyebrow", "触发路径"));
      const steps = [...(item.chain || [])];
      if (!steps.length || steps.at(-1) !== item.reason) steps.push(item.reason || "插件请求");
      if (item.retry_reason) steps.push(item.retry_reason);
      steps.forEach((text, i) => { if (i) path.append(el("span", "calls-path-arrow", "→")); path.append(el("span", "calls-path-step", text)); });
      const fields = el("dl", "calls-fields");
      field(fields, "输入 token", number(item.usage?.input)); field(fields, "其中缓存", number(item.usage?.input_cached));
      field(fields, "输出 token", number(item.usage?.output)); field(fields, "总 token", number(item.usage?.total));
      field(fields, "开始时间", stamp(item.started_at)); field(fields, "耗时", duration(item.duration_ms));
      field(fields, "调用链 ID", item.trace_id); field(fields, "请求次数", `第 ${item.attempt || 1} 次尝试`);
      field(fields, "会话标识", item.session_id || "编辑器 / 后台任务"); field(fields, "响应 ID", item.response_id);
      content.append(path, fields);
      if (openDecisions && item.session_id) {
        const decisions = el("button", "ghost small", "查看相关决策");
        decisions.onclick = () => openDecisions(item.session_id, item.trace_id || "");
        content.append(decisions);
      }
      if (item.coverage && item.coverage !== "direct") content.append(el("p", "calls-coverage-note", "这是原生 Agent / 工具循环的最终响应记录。中间轮次、内部重试及其用量未由当前公开钩子提供，不能视为整个任务的总消耗。"));
      if (item.usage?.partial) content.append(el("p", "calls-coverage-note", "Provider 只返回了部分用量字段，汇总保留已提供的数值。"));
      if (item.error) content.append(el("p", "calls-error", item.error));
      row.append(head, content); list.append(row);
    }
    $("calls-page").textContent = `${data.page} / ${data.pages}`;
    $("calls-prev").disabled = data.page <= 1; $("calls-next").disabled = data.page >= data.pages;
    $("calls-updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12: false})}`;
  }

  async function reload() {
    const request = ++revision;
    list.setAttribute("aria-busy", "true");
    $("calls-error-state").hidden = true;
    try {
      const result = await apiGet("model-calls", {config_scope: "", page, hours: Number($("calls-period").value),
        status: $("calls-status").value, purpose: $("calls-purpose").value, session: $("calls-session").value,
        provider: $("calls-provider").value, q: $("calls-search").value.trim()});
      if (request !== revision) return;
      // If a selected facet has disappeared, retain the selection until the user clears it.
      data = result; page = result.page;
      for (const [id, key, label] of [["calls-session", "sessions", "全部会话"], ["calls-purpose", "purposes", "全部用途"], ["calls-provider", "providers", "全部 Provider"]]) {
        const selected = $(id).value;
        const values = [...(result.facets?.[key] || [])];
        if (selected && !values.includes(selected)) values.push(selected);
        updateSelect(id, values, label);
      }
      renderSummary(); renderRows();
    } catch (error) {
      if (request !== revision) return;
      $("calls-error-state").hidden = false;
      $("calls-error-state").textContent = `读取失败：${error.message || error}。可点击刷新重试。`;
    } finally {
      if (request === revision) list.removeAttribute("aria-busy");
    }
  }

  for (const id of ["calls-period", "calls-status", "calls-purpose", "calls-session", "calls-provider"])
    $(id).addEventListener("change", () => { page = 1; reload(); });
  $("calls-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => {page = 1; reload();}, 250); });
  $("calls-refresh").addEventListener("click", reload);
  $("calls-prev").addEventListener("click", () => { page--; reload(); });
  $("calls-next").addEventListener("click", () => { page++; reload(); });
  $("calls-auto").addEventListener("change", () => {
    clearInterval(timer);
    if ($("calls-auto").checked) timer = setInterval(() => {
      if (root.classList.contains("active") && !document.hidden && !list.querySelector("details[open]") && list.getAttribute("aria-busy") !== "true") reload();
    }, 15000);
  });
  return { reload };
}
