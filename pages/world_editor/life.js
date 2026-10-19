/** A reading-first life board. Planning never masquerades as an execution receipt. */
const CLOSED = new Set(["completed", "abandoned", "expired"]);
const STATUS = {active: "待继续", working: "正在推进", waiting: "等合适的时候", blocked: "遇到了阻碍", paused: "暂时放下", completed: "已经完成", abandoned: "决定放下", expired: "念头淡去了"};
const KIND = {project: "个人项目", promise: "与人的约定", idea: "一时的念头"};
const TIME = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}) : "尚未安排";
const PROGRESS = item => item.steps?.length ? `已验证 ${item.steps.filter(s => s.status === "done").length}/${item.steps.length} 步` : "尚待安排";
const STATE = item => item.execution_review_required ? "等待核对执行" : ({environment: "等待执行环境", materials: "等待资料或素材", capability: "等待所需能力", uncertain: "等待核对执行", transient: "稍后重试"}[item.blocker] || STATUS[item.status] || item.status);

export function createLifePage({root, apiGet, apiPost, el, getSession, toast, openCalls, openMemories, openSettings}) {
  const $ = id => root.querySelector(`#${id}`);
  let data = {}, filter = "active", sort = "recent", request = 0, selected = null, opener = null;
  let drawerSession = "", drawerRevision = 0;
  const grid = $("life-grid"), drawer = $("life-drawer"), body = $("life-detail");
  function button(label, callback, cls = "ghost small") {
    const node = el("button", cls, label); node.type = "button"; node.addEventListener("click", callback); return node;
  }
  function line(label, text) {
    const field = el("div", "life-field"); field.append(el("span", "people-eyebrow", label), el("p", "", text || "尚未记录")); return field;
  }
  function bar(item) {
    if (!item.steps?.length) return el("span", "life-unplanned", "✧ 想法正在酝酿，尚无可验证步骤");
    const outer = el("div", "life-progress"), inner = el("span");
    const progress = Math.max(0, Math.min(100, Number(item.progress) || 0));
    outer.setAttribute("role", "progressbar"); outer.setAttribute("aria-label", "实际完成步骤"); outer.setAttribute("aria-valuenow", progress);
    outer.setAttribute("aria-valuemin", "0"); outer.setAttribute("aria-valuemax", "100"); inner.style.width = `${progress}%`; outer.append(inner); return outer;
  }
  function render() {
    const items = data.items || [], active = items.filter(item => !CLOSED.has(item.status));
    $("life-count").textContent = `${active.length} 件牵挂 · ${items.filter(item => item.status === "completed").length} 件完成`;
    $("life-now-title").textContent = data.activity?.label || "自由休息";
    $("life-now-reason").textContent = data.reason || data.choice?.reason || "在日常的节奏里，留一点不必赶路的时间。";
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
      const top = el("div", "life-card-top"); top.append(el("span", "people-eyebrow", KIND[item.kind] || "生活事项"), el("span", "life-badge", STATE(item)));
      const title = el("h3", "", item.text), motive = el("p", "life-motivation", item.motivation || "一直记着，想找个合适的时候继续。 ");
      const next = el("div", "life-card-next"); next.append(el("span", "people-eyebrow", item.obstacle ? "此刻的阻碍" : "下一小步"), el("p", "", item.obstacle || item.next_step || "还在想，从哪里开始"));
      title.title = item.text;
      const foot = el("div", "life-card-foot"); foot.append(el("span", "", TIME(item.updated_at || item.at)), el("b", "", PROGRESS(item)));
      card.append(top, title, motive, next, bar(item), foot); card.addEventListener("click", () => open(item, card)); grid.append(card);
    }
  }
  async function reload() {
    const revision = ++request, session = getSession();
    if (data.session && data.session !== session) {
      if (drawer.open) drawer.close();
      selected = null; data = {}; render();
      grid.replaceChildren(el("div", "life-empty", "正在读取这个会话的生活…"));
    }
    if (!session) { data = {}; render(); $("life-now-reason").textContent = "先在顶栏选择一个会话，看看她正在过怎样的一天。"; return; }
    try {
      const response = await apiGet("life", {session});
      if (revision !== request || getSession() !== session) return;
      data = response; render();
    } catch (error) { if (revision === request) { grid.replaceChildren(el("div", "life-empty", `读取失败：${error.message}`)); } }
  }
  async function mutate(operation, values = {}) {
    const session = drawerSession, revision = drawerRevision, id = selected?.id;
    if (!session || session !== getSession() || data.session !== session ||
        (selected && !(data.items || []).some(item => item.id === id))) { toast("会话已切换，请重新选择事项"); return; }
    const controls = [...body.querySelectorAll("button")];
    if (controls.some(node => node.disabled)) return;
    controls.forEach(node => { node.disabled = true; });
    try {
      const response = await apiPost("life/update", {session, id, operation, revision: selected?.revision, ...values});
      if (!response.ok) throw new Error(response.error || response.message || "未能保存");
      if (getSession() !== session) return;
      await reload();
      if (getSession() !== session || revision !== drawerRevision) return;
      if (drawer.open) drawer.close(); toast("生活事项已保存");
    } catch (error) { if (getSession() === session && revision === drawerRevision) toast(error.message || String(error)); }
    finally { controls.forEach(node => { node.disabled = false; }); }
  }
  function open(item, source) {
    drawerSession = data.session; drawerRevision++;
    selected = item; opener = source; body.replaceChildren(); $("life-drawer-title").textContent = item.text;
    body.append(line("为什么想做", item.motivation), line("接下来", item.next_step), bar(item));
    if (item.obstacle) body.append(line("阻碍", item.obstacle));
    if (item.recovery_condition) body.append(line("怎样继续", item.recovery_condition));
    if (item.remaining_condition) body.append(line("还有一部分需要条件", item.remaining_condition));
    if (item.resume_at) body.append(line("适合继续的时间", TIME(item.resume_at)));
    if (item.result) body.append(line("留下的结果", item.result));
    const steps = el("ol", "life-steps");
    for (const step of item.steps || []) {
      const row = el("li", step.status === "done" ? "done" : ""); row.append(el("b", "", step.text), el("small", "muted", step.status === "done" ? `实际做过 · ${TIME(step.finished_at)}` : step.action === "native_agent" ? "原生 Agent · 独立生活沙盒" : `安排中 · ${step.minutes} 分钟`)); steps.append(row);
    }
    if (steps.children.length) body.append(line("一件事，分成几小步", "进度按已执行步骤计算。"), steps);
    const artifacts = el("div", "life-artifacts");
    for (const artifact of item.artifacts || []) {
      const row = el("div", "life-artifact"); row.append(el("span", "people-eyebrow", "沙盒文件 · 已读回"), el("code", "", artifact.path), el("small", "muted", artifact.verification || TIME(artifact.verified_at))); artifacts.append(row);
    }
    if (artifacts.children.length) body.append(line("实际留下了什么", "文件保存在独立沙盒，沙盒过期后需要重新检查。样例不代表真实素材。"), artifacts);
    for (const execution of [...(item.executions || [])].reverse()) {
      const details = el("details", "life-execution"), summary = el("summary", "", `${TIME(execution.at)} · ${{running:"正在执行",succeeded:"小步已验证",unknown:"结果待核对",partial_failed:"部分成功",partial_waiting:"已有部分操作，待核对",returned:"已返回",failed:"本次失败"}[execution.status] || "历史执行"}`);
      details.append(summary);
      if (execution.accepted === false) details.append(el("p", "muted", "运行期间条件已变化，结果仅存档，没有覆盖当前安排。"));
      for (const event of execution.events || []) {
        const text = event.kind === "tool_start" ? `开始 · ${event.tool}` : event.kind === "tool_end" ? `${event.ok ? "已返回" : "未确认成功"} · ${event.tool}\n${event.result || ""}` : event.kind === "tool_unknown" ? `结果未知 · ${event.tool}\n${event.error}` : event.kind === "artifact" ? `文件已读回 · ${event.path}` : event.kind === "environment" ? `独立沙盒 · ${event.booter}` : "";
        if (text) details.append(el("p", "life-execution-line", text));
      }
      body.append(details);
    }
    const timeline = el("div", "life-timeline");
    for (const event of [...(item.history || [])].reverse()) {
      const row = el("div", "life-timeline-item"); row.append(el("time", "", TIME(event.at)), el("p", "", event.text));
      if (event.receipt) row.append(el("small", "muted", event.receipt.kind === "in_world_action" ? `世界内的实际活动 · ${event.receipt.minutes || 0} 分钟` : "已保存执行依据"));
      if (event.manual) row.append(el("small", "muted", "管理员调整")); timeline.append(row);
    }
    body.append(line("这件事怎样走到了现在", ""), timeline);
    const actions = el("div", "life-drawer-actions");
    if (!CLOSED.has(item.status)) {
      if (item.execution_review_required) actions.append(button("核对后继续…", () => {
        drawerRevision++; body.replaceChildren(); const reason = field("核对到的实际执行情况", "", "textarea");
        body.append(reason.wrap, el("p", "muted", "请先查看沙盒与上面的记录。继续后会保留历史，重新规划余下步骤，不会自动重放原步骤。"), button("记录核对并安排后续", () => mutate("acknowledge_execution", {reason: reason.input.value}), "primary"));
      }));
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
    drawerSession = data.session; drawerRevision++;
    selected = item; body.replaceChildren(); $("life-drawer-title").textContent = item ? "继续写这件事" : "添一件想做的事";
    const title = field("想做什么", item?.text), motive = field("为什么想做", item?.motivation, "textarea");
    const kind = el("select"); kind.setAttribute("aria-label", "事项类型"); for (const [value, name] of Object.entries(KIND)) kind.add(new Option(name, value));
    body.append(title.wrap, motive.wrap); if (!item) body.append(kind);
    body.append(el("p", "muted", "她会在清醒、有精力且空闲时，尝试安排能实际执行的小步。需要外部能力的约定会等待真实结果。"),
      button("保存事项", () => { if (!title.input.value.trim()) { title.input.reportValidity(); return; } mutate(item ? "edit" : "create", {text: title.input.value, motivation: motive.input.value, kind: kind.value}); }, "primary"));
    title.input.required = true; if (!drawer.open) drawer.showModal(); title.input.focus();
  }
  function defer(item) {
    drawerRevision++;
    body.replaceChildren(); $("life-drawer-title").textContent = "留到合适的时候";
    const time = field("什么时候再继续", "", "datetime-local"), reason = field("为什么先放一放", item.obstacle, "textarea");
    body.append(time.wrap, reason.wrap, button("保存时间", () => {
      const stamp = new Date(time.input.value).getTime() / 1000;
      if (!Number.isFinite(stamp) || stamp <= Date.now() / 1000) { toast("请选择将来的时间"); return; }
      mutate("postpone", {resume_at: stamp, reason: reason.input.value});
    }, "primary"));
  }
  function outcome(item) {
    drawerRevision++;
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
  $("life-settings").addEventListener("click", () => { $("life-tools").removeAttribute("open"); openSettings(data.execution); });
  $("life-collection").addEventListener("click", event => {
    $("life-tools").removeAttribute("open");
    selected = null; opener = event.currentTarget; body.replaceChildren(); $("life-drawer-title").textContent = "日子留下的东西";
    drawerSession = data.session; drawerRevision++;
    const interests = Object.values(data.interests || {}).filter(item => (item.days || []).length >= 3 && item.sessions >= 3 && item.minutes >= 45);
    body.append(line("日子留下的活动经验", interests.length ? "多日的实际投入可提供温和节奏参考；做得多不等于喜欢，原有人设始终优先。" : "还在积累；一时兴起不会改变她的长期喜好。"));
    for (const item of interests) {
      const wrap = line(item.label, `${item.days.length} 天 · ${item.sessions} 次 · 累计 ${Math.round(item.minutes)} 分钟\n最近来源：${(item.episodes || []).slice(-3).map(row => `${row.day} ${row.period}`).join("、") || "历史活动记录"}`);
      const action = Object.keys(data.interests || {}).find(key => data.interests[key] === item);
      wrap.append(button("不再沿用这条活动经验", async () => {
        const session = drawerSession, version = drawerRevision;
        if (getSession() !== session) return;
        const response = await apiPost("life/update", {session, operation: "forget_experience", action});
        if (!response.ok) { toast(response.error || "未能整理"); return; }
        await reload(); if (getSession() === session && version === drawerRevision) { drawer.close(); toast("已清理活动经验，生活记录保留"); }
      })); body.append(wrap);
    }
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
    container.append(next, el("small", "muted", `${STATE(focus)} · ${PROGRESS(focus)}`));
  }
}

export function openLifeSettings({world, execution = {}, el, openCustomDialog, markDirty}) {
  const limits = {...world.limits}, memory = {...world.memory};
  const life = {...world.life_execution, trusted_tools: {...world.life_execution?.trusted_tools}};
  const controls = [];
  openCustomDialog({title: "生活的节奏与后台预算", confirmText: "应用设置", hint: "应用后使用顶栏「保存」写入配置。0 表示不设对应预算上限。", build: body => {
    for (const [key, label, fallback, min, max] of [
      ["life_matter_active_max", "活跃事项上限", 6, 1, 20], ["life_matter_daily_minutes", "每件事每天最多投入（分钟）", 90, 5, 480],
      ["life_matter_interval_minutes", "再次推进前的间隔（分钟）", 60, 5, 1440], ["background_daily_calls", "每天后台调用预算", 120, 0, 10000],
      ["background_daily_tokens", "每天后台 token 预算", 0, 0, 100000000], ["background_concurrency", "同时运行的后台模型请求", 1, 1, 12]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "number"; input.min = min; input.max = max; input.value = limits[key] ?? fallback;
      wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { if (!input.checkValidity()) throw new Error(`${label}超出范围`); limits[key] = Number(input.value); });
    }
    for (const [object, key, label, fallback] of [[limits, "life_matter_enabled", "自动推进生活事项", true], [limits, "life_matter_self_start", "空闲时会自己产生个人打算", true], [memory, "semantic_enabled", "启用可选语义召回", false], [memory, "life_rerank", "主动查询生活记忆时重排（额外一次判断请求）", false]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "checkbox"; input.checked = object[key] ?? fallback; wrap.append(input, el("span", "", label)); body.append(wrap); controls.push(() => { object[key] = input.checked; });
    }
    body.append(el("p", "muted", "Token 缺失时保留保守的预算占位，界面仍标为未知。语义索引失败时保留记忆并使用关键词检索。"));
    body.append(el("h3", "", "生活的独立执行环境"), el("p", "muted", execution.ready ? `原生沙盒 · ${execution.booter}，执行时会检查连接。` : execution.reason || "等待接入 AstrBot 原生沙盒"));
    const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = life.enabled ?? true;
    const enabledLabel = el("label", "field"); enabledLabel.append(enabled, el("span", "", "允许自主生活使用独立沙盒")); body.append(enabledLabel);
    const sandbox = el("select"); sandbox.setAttribute("aria-label", "生活沙盒环境");
    for (const [value, label] of [["inherit", "沿用 AstrBot 沙盒服务设置"], ["shipyard_neo", "Shipyard Neo"], ["shipyard", "Shipyard"], ["boxlite", "BoxLite（需要宿主安装支持）"], ["cua", "CUA 独立电脑沙盒"]]) sandbox.add(new Option(label, value));
    sandbox.value = life.sandbox || "inherit"; body.append(sandbox);
    controls.push(() => { life.enabled = enabled.checked; life.sandbox = sandbox.value; });
    for (const [key, label, fallback, min, max] of [["max_steps", "每次原生 Agent 最多轮数", 8, 1, 30], ["timeout_seconds", "每次生活执行最长等待（秒）", 300, 30, 1800], ["tool_timeout_seconds", "单个工具最长等待（秒）", 90, 5, 300], ["max_sandboxes", "生活沙盒总上限（以全局设置为准）", 1, 1, 8], ["idle_release_seconds", "生活沙盒空闲回收（秒，0 关闭）", 300, 0, 86400]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "number"; input.min = min; input.max = max; input.value = life[key] ?? fallback;
      wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { if (!input.checkValidity()) throw new Error(`${label}超出范围`); life[key] = Number(input.value); });
    }
    body.append(el("p", "muted", "默认只保留一个生活环境，空闲五分钟后回收；需要腾出名额时优先回收闲置环境。回收可能丢失临时文件，后续会先检查再继续。重要产物请另行保存。旧 Shipyard 还依赖服务端 TTL，不能保证立即释放内存。"));
    body.append(el("p", "muted", "QQ 继续使用原来的电脑配置。生活始终创建独立沙盒，连接失败不会转用服务器 Shell。沙盒服务应避免挂载服务器业务目录或注入无关凭据。"));
    const toolSection = el("details", "life-tool-settings"); toolSection.append(el("summary", "", "生活可用工具与宿主授权"));
    toolSection.append(el("p", "muted", "原生电脑工具在沙盒内执行。其他插件和 MCP 工具仍在宿主或外部服务执行；勾选表示信任其真实权限，无法通过生活沙盒约束它。只授权已经了解的工具。工具定义变化会自动撤销旧授权。"));
    for (const tool of execution.tools || []) {
      const wrap = el("label", "life-tool-option"), check = el("input"); check.type = "checkbox";
      check.checked = tool.scope === "sandbox" || life.trusted_tools[tool.name] === tool.fingerprint;
      check.disabled = tool.scope === "sandbox" || (tool.reason && !tool.reason.startsWith("需要单独信任"));
      const text = el("span"); text.append(el("b", "", tool.name), el("small", "muted", tool.scope === "sandbox" ? "独立沙盒" : tool.reason || "已信任 · 在宿主执行"));
      wrap.append(check, text); toolSection.append(wrap);
      if (tool.scope !== "sandbox" && !check.disabled) controls.push(() => { if (check.checked) life.trusted_tools[tool.name] = tool.fingerprint; else delete life.trusted_tools[tool.name]; });
    }
    body.append(toolSection);
    for (const [key, label] of [["semantic_endpoint", "嵌入服务 API 基址（含 /v1）"], ["semantic_model", "嵌入模型名称"], ["semantic_api_key_env", "保存 API Key 的服务器环境变量名"]]) {
      const wrap = el("label", "field"), input = el("input"); input.value = memory[key] || ""; wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { memory[key] = input.value.trim(); });
    }
  }, onSubmit: () => { controls.forEach(read => read()); world.limits = limits; world.memory = memory; world.life_execution = life; markDirty(); return true; }});
}
