/** A reading-first life board. Planning never masquerades as an execution receipt. */
const CLOSED = new Set(["completed", "abandoned", "expired"]);
const STATUS = {active: "待继续", working: "正在推进", waiting: "等合适的时候", blocked: "遇到了阻碍", paused: "暂时放下", completed: "已经完成", abandoned: "决定放下", expired: "念头淡去了"};
const KIND = {project: "个人项目", promise: "与人的约定", idea: "一时的念头"};
const TIME = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}) : "尚未安排";

export function createLifePage({root, apiGet, apiPost, el, getSession, toast, openCalls, openMemories, openSettings}) {
  const $ = id => root.querySelector(`#${id}`);
  let data = {}, filter = "active", sort = "recent", request = 0, selected = null, opener = null;
  const grid = $("life-grid"), drawer = $("life-drawer"), body = $("life-detail");
  function button(label, callback, cls = "ghost small") {
    const node = el("button", cls, label); node.type = "button"; node.addEventListener("click", callback); return node;
  }
  function line(label, text) {
    const field = el("div", "life-field"); field.append(el("span", "people-eyebrow", label), el("p", "", text || "尚未记录")); return field;
  }
  function bar(item) {
    const outer = el("div", "life-progress"), inner = el("span");
    const progress = Math.max(0, Math.min(100, Number(item.progress) || 0));
    outer.setAttribute("role", "progressbar"); outer.setAttribute("aria-label", "实际完成步骤"); outer.setAttribute("aria-valuenow", progress);
    outer.setAttribute("aria-valuemin", "0"); outer.setAttribute("aria-valuemax", "100"); inner.style.width = `${progress}%`; outer.append(inner); return outer;
  }
  function render() {
    const items = data.items || [], active = items.filter(item => !CLOSED.has(item.status));
    $("life-count").textContent = `${active.length} 件牵挂 · ${items.filter(item => item.status === "completed").length} 件完成`;
    $("life-now-title").textContent = data.activity?.label || "自由休息";
    $("life-now-reason").textContent = data.reason || "在日常的节奏里，留一点不必赶路的时间。";
    $("life-recovery").textContent = data.recovery?.note || "";
    $("life-recovery").classList.toggle("hidden", !data.recovery?.note);
    const featured = active.find(item => item.status === "working") || active.find(item => item.status === "active");
    $("life-next-title").textContent = featured?.text || "有空再开始一件喜欢的事";
    $("life-next-text").textContent = featured?.next_step || featured?.motivation || "从对话里产生的打算，也会慢慢成为她生活的一部分。";
    let shown = items.filter(item => filter === "all" || (filter === "active" ? !CLOSED.has(item.status) : filter === "closed" ? CLOSED.has(item.status) : item.status === "blocked" || item.status === "waiting"));
    const q = $("life-search").value.trim().toLocaleLowerCase();
    if (q) shown = shown.filter(item => [item.text, item.motivation, item.next_step, item.obstacle].join(" ").toLocaleLowerCase().includes(q));
    shown.sort((a, b) => sort === "progress" ? (b.progress || 0) - (a.progress || 0) : sort === "oldest" ? a.at - b.at : (b.updated_at || b.at) - (a.updated_at || a.at));
    grid.replaceChildren();
    if (!shown.length) {
      const empty = el("div", "life-empty"); empty.append(el("span", "", "✦"), el("h3", "让一件小事，慢慢有了下文"),
        el("p", "muted", items.length ? "这个筛选下没有事项。" : "她在对话里记下的打算会出现在这里。也可以为她添一件想持续做的事。")); grid.append(empty);
    }
    for (const item of shown) {
      const card = el("button", `life-card is-${item.status}`); card.type = "button"; card.dataset.id = item.id;
      const top = el("div", "life-card-top"); top.append(el("span", "people-eyebrow", KIND[item.kind] || "生活事项"), el("span", "life-badge", STATUS[item.status] || item.status));
      const title = el("h3", "", item.text), motive = el("p", "life-motivation", item.motivation || "一直记着，想找个合适的时候继续。 ");
      const next = el("div", "life-card-next"); next.append(el("span", "people-eyebrow", item.obstacle ? "此刻的阻碍" : "下一小步"), el("p", "", item.obstacle || item.next_step || "还在想，从哪里开始"));
      const foot = el("div", "life-card-foot"); foot.append(el("span", "", TIME(item.updated_at || item.at)), el("b", "", `${item.progress || 0}%`));
      card.append(top, title, motive, next, bar(item), foot); card.addEventListener("click", () => open(item, card)); grid.append(card);
    }
  }
  async function reload() {
    const revision = ++request, session = getSession();
    if (data.session && data.session !== session && drawer.open) drawer.close();
    if (!session) { data = {}; render(); $("life-now-reason").textContent = "先在顶栏选择一个会话，看看她正在过怎样的一天。"; return; }
    try {
      const response = await apiGet("life", {session});
      if (revision !== request || getSession() !== session) return;
      data = response; render();
    } catch (error) { if (revision === request) { grid.replaceChildren(el("div", "life-empty", `读取失败：${error.message}`)); } }
  }
  async function mutate(operation, values = {}) {
    if (selected && !(data.items || []).some(item => item.id === selected.id)) { toast("会话已切换，请重新选择事项"); return; }
    try {
      const response = await apiPost("life/update", {session: getSession(), id: selected?.id, operation, ...values});
      if (!response.ok) throw new Error(response.error || response.message || "未能保存");
      await reload(); if (drawer.open) drawer.close(); toast("生活事项已保存");
    } catch (error) { toast(error.message || String(error)); }
  }
  function open(item, source) {
    selected = item; opener = source; body.replaceChildren(); $("life-drawer-title").textContent = item.text;
    body.append(line("为什么想做", item.motivation), line("接下来", item.next_step), bar(item));
    if (item.obstacle) body.append(line("阻碍", item.obstacle));
    if (item.resume_at) body.append(line("适合继续的时间", TIME(item.resume_at)));
    if (item.result) body.append(line("留下的结果", item.result));
    const steps = el("ol", "life-steps");
    for (const step of item.steps || []) {
      const row = el("li", step.status === "done" ? "done" : ""); row.append(el("b", "", step.text), el("small", "muted", step.status === "done" ? `实际做过 · ${TIME(step.finished_at)}` : `安排中 · ${step.minutes} 分钟`)); steps.append(row);
    }
    if (steps.children.length) body.append(line("一件事，分成几小步", "进度按已执行步骤计算。"), steps);
    const timeline = el("div", "life-timeline");
    for (const event of [...(item.history || [])].reverse()) {
      const row = el("div", "life-timeline-item"); row.append(el("time", "", TIME(event.at)), el("p", "", event.text));
      if (event.receipt) row.append(el("small", "muted", event.receipt.kind === "in_world_action" ? `世界内的实际活动 · ${event.receipt.minutes || 0} 分钟` : "已保存执行依据"));
      if (event.manual) row.append(el("small", "muted", "管理员调整")); timeline.append(row);
    }
    body.append(line("这件事怎样走到了现在", ""), timeline);
    const actions = el("div", "life-drawer-actions");
    if (!CLOSED.has(item.status)) {
      actions.append(button(item.status === "paused" ? "继续这件事" : "暂时放下", () => mutate(item.status === "paused" ? "resume" : "pause")),
        button("晚一点继续", () => defer(item)), button("编辑", () => form(item)), button("确认结果…", () => outcome(item)));
    }
    actions.append(button("相关模型调用 ↗", () => { drawer.close(); openCalls(item.id); }), button("相关记忆 ↗", () => { drawer.close(); openMemories(item.text); }));
    body.append(actions, el("small", "muted life-id", item.id));
    if (!drawer.open) drawer.showModal();
  }
  function field(label, value = "", type = "text") {
    const wrap = el("label", "field"), input = el(type === "textarea" ? "textarea" : "input");
    if (type !== "textarea") input.type = type;
    input.value = value; input.maxLength = 300; wrap.append(el("span", "", label), input); return {wrap, input};
  }
  function form(item = null) {
    selected = item; body.replaceChildren(); $("life-drawer-title").textContent = item ? "继续写这件事" : "添一件想做的事";
    const title = field("想做什么", item?.text), motive = field("为什么想做", item?.motivation, "textarea");
    const kind = el("select"); kind.setAttribute("aria-label", "事项类型"); for (const [value, name] of Object.entries(KIND)) kind.add(new Option(name, value));
    body.append(title.wrap, motive.wrap); if (!item) body.append(kind);
    body.append(el("p", "muted", "她会在清醒、有精力且空闲时，尝试安排能实际执行的小步。需要外部能力的约定会等待真实结果。"),
      button("保存事项", () => { if (!title.input.value.trim()) { title.input.reportValidity(); return; } mutate(item ? "edit" : "create", {text: title.input.value, motivation: motive.input.value, kind: kind.value}); }, "primary"));
    title.input.required = true; if (!drawer.open) drawer.showModal(); title.input.focus();
  }
  function defer(item) {
    body.replaceChildren(); $("life-drawer-title").textContent = "留到合适的时候";
    const time = field("什么时候再继续", "", "datetime-local"), reason = field("为什么先放一放", item.obstacle, "textarea");
    body.append(time.wrap, reason.wrap, button("保存时间", () => {
      const stamp = new Date(time.input.value).getTime() / 1000;
      if (!Number.isFinite(stamp) || stamp <= Date.now() / 1000) { toast("请选择将来的时间"); return; }
      mutate("postpone", {resume_at: stamp, reason: reason.input.value});
    }, "primary"));
  }
  function outcome(item) {
    body.replaceChildren(); $("life-drawer-title").textContent = "留下真实的结果";
    const reason = field("实际结果或放下的原因", "", "textarea");
    body.append(reason.wrap, el("p", "muted", "手动确认会明确记录为管理员调整，不会被写成模型或工具完成的成果。"),
      button("手动确认已完成", () => mutate("complete", {reason: reason.input.value}), "primary"),
      button("决定放弃", () => mutate("abandon", {reason: reason.input.value})));
  }
  $("life-close").addEventListener("click", () => drawer.close());
  drawer.addEventListener("close", () => {
    const target = opener?.isConnected ? opener : [...grid.children].find(node => node.dataset.id === selected?.id);
    (target || $("life-add")).focus();
  });
  drawer.addEventListener("click", event => {
    const r = drawer.getBoundingClientRect();
    if (event.target === drawer && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom)) drawer.close();
  });
  $("life-refresh").addEventListener("click", () => { $("life-tools").removeAttribute("open"); reload(); }); $("life-add").addEventListener("click", () => form());
  $("life-settings").addEventListener("click", () => { $("life-tools").removeAttribute("open"); openSettings(); });
  $("life-collection").addEventListener("click", event => {
    $("life-tools").removeAttribute("open");
    selected = null; opener = event.currentTarget; body.replaceChildren(); $("life-drawer-title").textContent = "日子留下的东西";
    const interests = Object.values(data.interests || {}).filter(item => (item.days || []).length >= 3 && item.sessions >= 3 && item.minutes >= 45);
    body.append(line("慢慢形成的兴趣", interests.length ? "至少三个不同日子里的实际投入，才会在后续选择中成为参考。" : "还在积累；一时兴起不会改变她的长期喜好。"));
    for (const item of interests) body.append(line(item.label, `${item.days.length} 天 · ${item.sessions} 次 · 累计 ${Math.round(item.minutes)} 分钟`));
    body.append(line("生活的阶段记录", (data.artifacts || []).length ? "这些记录证明世界内做过的活动；外部文件、发送和购物以真实结果为准。" : "完成事项后，实际活动的阶段结果会留在这里。"));
    for (const item of [...(data.artifacts || [])].reverse()) body.append(line(item.label, `${item.result}\n${TIME(item.at)}`));
    const budget = data.budget || {};
    body.append(line("今日后台模型用量", `${budget.calls || 0} 次请求 · 已知 ${Number(budget.tokens || 0).toLocaleString()} token\n${budget.unknown || 0} 次未提供完整用量 · 保守占位 ${Number(budget.reserved || 0).toLocaleString()} token`),
      line("语义召回", data.semantic || "未启用"));
    drawer.showModal();
  });
  $("life-filter").addEventListener("change", event => { filter = event.target.value; render(); });
  $("life-sort").addEventListener("change", event => { sort = event.target.value; render(); });
  $("life-search").addEventListener("input", render);
  return {reload};
}

export function renderLifeFocus(container, data, {el, open}) {
  container.replaceChildren();
  const active = (data.life_matters || []).filter(item => !CLOSED.has(item.status));
  const focus = active.find(item => item.status === "working") || active[0];
  const heading = el("div", "life-focus-heading"); heading.append(el("span", "people-eyebrow", "日子在继续"));
  const link = el("button", "ghost small", "生活事项 ↗"); link.type = "button"; link.addEventListener("click", open); heading.append(link);
  container.append(heading, el("h3", "", focus?.text || data.activity?.label || "慢慢过好今天"),
    el("p", "", focus?.motivation || data.activity_reason || "在自己的节奏里活动与休息。"));
  if (focus) {
    const next = el("div", "life-focus-next"); next.append(el("span", "people-eyebrow", focus.obstacle ? "此刻的阻碍" : "下一小步"), el("span", "", focus.obstacle || focus.next_step || "还在想，从哪里开始"));
    container.append(next, el("small", "muted", `${STATUS[focus.status]} · 已执行进度 ${focus.progress || 0}%`));
  }
}

export function openLifeSettings({world, el, openCustomDialog, markDirty}) {
  const limits = {...world.limits}, memory = {...world.memory};
  const controls = [];
  openCustomDialog({title: "生活的节奏与后台预算", confirmText: "应用设置", hint: "应用后使用顶栏「保存」写入配置。0 表示不设对应预算上限。", build: body => {
    for (const [key, label, fallback, min, max] of [
      ["life_matter_active_max", "活跃事项上限", 6, 1, 20], ["life_matter_daily_minutes", "每件事每天最多投入（分钟）", 90, 5, 480],
      ["life_matter_interval_minutes", "再次推进前的间隔（分钟）", 60, 5, 1440], ["background_daily_calls", "每天后台调用预算", 120, 0, 10000],
      ["background_daily_tokens", "每天后台 token 预算", 0, 0, 100000000], ["background_concurrency", "同时运行的后台模型请求", 3, 1, 12]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "number"; input.min = min; input.max = max; input.value = limits[key] ?? fallback;
      wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { if (!input.checkValidity()) throw new Error(`${label}超出范围`); limits[key] = Number(input.value); });
    }
    for (const [object, key, label, fallback] of [[limits, "life_matter_enabled", "自动推进生活事项", true], [limits, "life_matter_self_start", "空闲时会自己产生个人打算", true], [memory, "semantic_enabled", "启用可选语义召回", false]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "checkbox"; input.checked = object[key] ?? fallback; wrap.append(input, el("span", "", label)); body.append(wrap); controls.push(() => { object[key] = input.checked; });
    }
    body.append(el("p", "muted", "Token 缺失时保留保守的预算占位，界面仍标为未知。语义索引失败时保留记忆并使用关键词检索。"));
    for (const [key, label] of [["semantic_endpoint", "嵌入服务 API 基址（含 /v1）"], ["semantic_model", "嵌入模型名称"], ["semantic_api_key_env", "保存 API Key 的服务器环境变量名"]]) {
      const wrap = el("label", "field"), input = el("input"); input.value = memory[key] || ""; wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { memory[key] = input.value.trim(); });
    }
  }, onSubmit: () => { controls.forEach(read => read()); world.limits = limits; world.memory = memory; markDirty(); return true; }});
}
