/** A single life workspace. Existing editors are retained as secondary drawers. */
export function mountLifeWorkspace({root, el, onView, onSave}) {
  const $ = id => document.getElementById(id);
  const header = el("div", "life-workspace-head"), title = el("div");
  title.append(el("span", "people-eyebrow", "日子在继续"), el("h2", "", "她的生活"));
  const nav = el("div", "life-view-nav"); nav.setAttribute("role", "tablist"); nav.setAttribute("aria-label", "生活视图");
  header.append(title, nav);
  const today = el("div", "life-view life-today"); today.id = "life-view-today";
  const concerns = el("div", "life-view life-concerns"); concerns.id = "life-view-concerns";
  concerns.append($("life-toolbar" ) || root.querySelector(".life-toolbar"), $("life-grid"));
  const experiences = $("tab-events"); experiences.classList.remove("tab"); experiences.classList.add("life-view", "life-experiences");
  const eventTools = experiences.querySelector(".page-head");
  const schedules = $("tab-schedules"); schedules.classList.remove("tab"); schedules.classList.add("active", "life-schedule-editor");
  const overview = el("div", "life-overview-grid"), agenda = el("section", "life-day-card"), aside = el("aside", "life-day-card life-context-card");
  const agendaHead = el("div", "life-section-head"); agendaHead.append(el("h3", "", "今天的安排"));
  const editSchedule = el("button", "ghost small", "编辑日程 ↗"); editSchedule.type = "button"; agendaHead.append(editSchedule);
  const agendaBody = el("div", "life-agenda"); agendaBody.id = "life-agenda"; agenda.append(agendaHead, agendaBody);
  const rhythmHead = el("div", "life-section-head"); rhythmHead.append(el("h3", "", "慢慢形成的节奏"));
  const rhythmBody = el("div", "life-rhythm-list"); rhythmBody.id = "life-rhythms"; aside.append(rhythmHead, rhythmBody);
  overview.append(agenda, aside);
  const recent = el("section", "life-recent"), recentHead = el("div", "life-section-head");
  recentHead.append(el("h3", "", "最近的经历"));
  const seeAll = el("button", "ghost small", "展开经历 →"); seeAll.type = "button";
  const recentBody = el("div", "life-recent-grid"); recentBody.id = "life-recent-grid";
  recentHead.append(seeAll); recent.append(recentHead, recentBody); today.append(overview, recent);
  const help = el("details", "life-explanation"); help.append(el("summary", "", "安排、牵挂和经历，怎样连成生活？"),
    el("p", "", "日程安排什么时候做；牵挂保存想跨天继续的事；经历记录生活中发生的变化。一次练习可以按日程推进同一件牵挂，遇到意外就暂时放下，之后接着做。普通经历可以自然结束，自由时间也不必填满。"));
  root.prepend(header); root.querySelector(".life-opening").after(today, concerns, experiences, help);
  const buttons = new Map(), panels = {today, concerns, experiences};
  function select(view = "today") {
    if (!(view in panels)) view = "today";
    for (const [key, panel] of Object.entries(panels)) {
      panel.hidden = key !== view;
      panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `life-view-button-${key}`);
      const button = buttons.get(key); button.setAttribute("aria-selected", String(key === view)); button.tabIndex = key === view ? 0 : -1;
    }
    root.dataset.lifeView = view; onView(view);
  }
  for (const [view, label] of [["today", "今日"], ["concerns", "牵挂"], ["experiences", "经历"]]) {
    const button = el("button", "life-view-button", label); button.type = "button"; button.id = `life-view-button-${view}`;
    button.dataset.lifeView = view; button.setAttribute("role", "tab"); button.setAttribute("aria-controls", panels[view].id);
    button.onclick = () => select(view); nav.append(button); buttons.set(view, button);
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); const keys = [...buttons.keys()], index = keys.indexOf(view);
      const next = event.key === "Home" ? 0 : event.key === "End" ? 2 : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      select(keys[next]); buttons.get(keys[next]).focus();
    });
  }
  function manager(name, content, trigger) {
    const dialog = el("dialog", "life-manager"); dialog.setAttribute("aria-label", name);
    const head = el("header", "life-drawer-head"); head.append(el("h2", "", name));
    const save = el("button", "primary small life-manager-save", "保存设置"); save.type = "button";
    save.onclick = async () => { save.disabled = true; try { await onSave(); } finally { save.disabled = false; } }; head.append(save);
    const close = el("button", "ghost small", "完成 ×"); close.type = "button"; close.onclick = () => dialog.close(); head.append(close);
    const scope = el("div", "life-manager-scope");
    const backdrop = el("div", "life-manager-backdrop"); backdrop.hidden = true; backdrop.onclick = () => dialog.close();
    const body = el("div", "life-manager-body"); body.append(content); dialog.append(head, scope, body); root.append(backdrop, dialog);
    let opener = trigger;
    let moved = [];
    const open = () => {
      if (dialog.open) return;
      for (const other of document.querySelectorAll('.life-manager[open]')) other.close();
      opener = document.activeElement;
      for (const node of [$("config-scope")?.closest("label"), $("scope-inheritance"), $("save-state")].filter(Boolean)) {
        const marker = document.createComment("life-manager-control"); node.before(marker); moved.push({node, marker}); scope.append(node);
      }
      backdrop.hidden = false; dialog.show(); close.focus(); onView(name === "日程管理" ? "schedules" : "event-tools");
    };
    trigger.onclick = open;
    dialog.addEventListener("close", () => {
      backdrop.hidden = true;
      for (const {node, marker} of moved) marker.replaceWith(node); moved = [];
      if (opener?.isConnected) opener.focus(); onView(root.dataset.lifeView);
    });
    document.addEventListener("keydown", event => {
      if (!dialog.open) return;
      if (document.querySelector('.modal:not(.hidden), dialog[open]:not(.life-manager)')) return;
      if (event.key === "Escape") { event.preventDefault(); dialog.close(); }
      if (event.key === "Tab") {
        const focusable = [...dialog.querySelectorAll('button, input, select, textarea, summary, [tabindex="0"]')].filter(e => !e.disabled && e.getClientRects().length);
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    dialog.addEventListener("click", event => { const r = dialog.getBoundingClientRect(); if (event.target === dialog && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom)) dialog.close(); });
    return {open, dialog};
  }
  const scheduleManager = manager("日程管理", schedules, editSchedule);
  const eventTrigger = el("button", "ghost small", "投递与事件设置…"); eventTrigger.type = "button";
  const eventHead = el("div", "life-section-head"); eventHead.append(el("h3", "", "遇见的事，留下的下文"), eventTrigger); experiences.prepend(eventHead);
  manager("事件管理", eventTools, eventTrigger);
  seeAll.onclick = () => select("experiences");
  // Set initial visibility without loading until the life page is opened.
  for (const [view, panel] of Object.entries(panels)) { panel.hidden = view !== "today"; panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", `life-view-button-${view}`); buttons.get(view).setAttribute("aria-selected", String(view === "today")); buttons.get(view).tabIndex = view === "today" ? 0 : -1; }
  root.dataset.lifeView = "today";
  return {
    select, openSchedules: scheduleManager.open,
    render(data, openMatter) {
      title.querySelector(".people-eyebrow").textContent = data.day ? `${data.day.replaceAll("-", " / ")} · 日子在继续` : "日子在继续";
      agendaBody.replaceChildren();
      const labels = {upcoming: "待开始", past: "时刻已过", started: "已开始过", delayed: "暂缓", cancelled: "今天放下"};
      for (const item of data.agenda || []) {
        const row = el("button", `life-agenda-row is-${item.status}`); row.type = "button";
        const text = el("div"); text.append(el("strong", "", item.title), el("small", "muted", item.matter_id ? "接续已有牵挂 · 共用同一份进度" : "日常安排"));
        row.append(el("time", "", item.time), text, el("span", "life-badge", labels[item.status] || "已安排"));
        row.onclick = () => item.matter_id && (data.items || []).some(m => m.id === item.matter_id) ? openMatter(item.matter_id, row) : scheduleManager.open(); agendaBody.append(row);
      }
      if (!agendaBody.children.length) agendaBody.append(el("p", "life-soft-empty", data.session ? "今天没有固定安排。她仍然可以自由活动、休息，或接续一件牵挂。" : "先选择会话，看看她的一天。"));
      rhythmBody.replaceChildren();
      for (const item of data.rhythms || []) {
        const card = el("details", "life-rhythm"); card.append(el("summary", "", `${item.period} · ${item.label}`), el("p", "", item.explanation));
        for (const week of item.weekdays || []) card.append(el("small", "life-linked-label", `周${["一", "二", "三", "四", "五", "六", "日"][week.weekday]} · ${week.days.length} 个不同日期的重复节奏`));
        for (const row of item.evidence || []) card.append(el("small", "muted", `${row.day} · ${row.period} · ${Math.round(row.minutes || 0)} 分钟`));
        rhythmBody.append(card);
      }
      if (!rhythmBody.children.length) rhythmBody.append(el("p", "life-soft-empty", "节奏需要真实的多日积累。现在可以随心度过，不必为了形成习惯反复做同一件事。"));
      if (data.resume?.reason) rhythmBody.append(el("p", "life-resume-note", `暂时放下：${data.resume.reason}。条件合适时接着做。`));
      recentBody.replaceChildren();
      for (const item of (data.experiences || []).slice(0, 3)) {
        const card = el("button", "life-experience-card"); card.type = "button";
        card.append(el("span", "people-eyebrow", item.status === "closed" ? "已经历" : "还有下文"), el("h3", "", item.title), el("p", "", item.result || item.next || "这一段经历正在继续。"));
        if (item.matter_id) card.append(el("small", "life-linked-label", "已关联一件牵挂 ↗"));
        card.onclick = () => item.matter_id && (data.items || []).some(m => m.id === item.matter_id) ? openMatter(item.matter_id, card) : select("experiences"); recentBody.append(card);
      }
      if (!recentBody.children.length) recentBody.append(el("p", "life-soft-empty", "平静的一天也属于生活。新的经历会留在这里。"));
    }
  };
}
