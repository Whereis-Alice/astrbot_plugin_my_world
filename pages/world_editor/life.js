/** A reading-first life board for activities in the character's world. */
const CLOSED = new Set(["completed", "abandoned", "expired"]);
const STATUS = {active: "待继续", working: "正在推进", waiting: "等合适的时候", blocked: "遇到了阻碍", paused: "暂时放下", completed: "已经完成", abandoned: "决定放下", expired: "念头淡去了"};
const KIND = {project: "个人项目", promise: "与人的约定", idea: "一时的念头"};
const TIME = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}) : "尚未安排";
const PROGRESS = item => item.steps?.length ? `已进行 ${item.steps.filter(s => s.status === "done").length}/${item.steps.length} 步` : "尚待安排";
const STATE = item => ({evidence: "等待约定兑现", transient: "稍后再想"}[item.blocker] || STATUS[item.status] || item.status);

export function createLifePage({root, apiGet, apiPost, el, getSession, toast, openCalls, openMemories, openSettings, openBrowseSettings, workspace, onData}) {
  const $ = id => root.querySelector(`#${id}`);
  let data = {}, filter = "active", sort = "recent", request = 0, selected = null, opener = null;
  let drawerSession = "", drawerScope = "", drawerRevision = 0;
  let weekData = {}, weekRequest = 0, weekSession = "", weekLoading = false;
  const weekPending = new Set();
  const generationExtras = el("details", "generation-extra");
  generationExtras.append(el("summary", "muted", "生成日历／日记的额外提示词（可选）"));
  const generationHint = document.createElement("textarea"); generationHint.rows = 2; generationHint.maxLength = 2000;
  generationHint.setAttribute("aria-label", "日历／日记额外提示词");
  generationHint.placeholder = "本次生成偏好；日记仍只依据已保存的经历。";
  generationExtras.append(generationHint);
  $("life-week-generate-calendar").parentElement.after(generationExtras);
  function updateWeekControls() {
    const ready = !weekLoading && weekSession === getSession() && Boolean(weekData.week_start);
    for (const id of ["life-week-prev", "life-week-next"]) $(id).disabled = !ready;
    const calendar = $("life-week-generate-calendar"), diary = $("life-week-generate-diary");
    calendar.disabled = !ready || weekPending.has("calendar");
    diary.disabled = !ready || !weekData.today || weekPending.has("diary");
    const year = Number(String(weekData.week_start || "").slice(0, 4));
    calendar.textContent = weekPending.has("calendar") ? "正在生成…" : `生成${year ? ` ${year} 年` : "本年"}日历`;
    diary.textContent = weekPending.has("diary") ? "正在补写…" : "补写前一天日记";
  }
  const grid = $("life-grid"), drawer = $("life-drawer"), body = $("life-detail");
  function button(label, callback, cls = "ghost small") {
    const node = el("button", cls, label); node.type = "button"; node.addEventListener("click", callback); return node;
  }
  function line(label, text) {
    const field = el("div", "life-field"); field.append(el("span", "people-eyebrow", label), el("p", "", text || "尚未记录")); return field;
  }
  function bar(item) {
    if (!item.steps?.length) return el("span", "life-unplanned", "✧ 想法正在酝酿，尚待安排小步");
    const outer = el("div", "life-progress"), inner = el("span");
    const progress = Math.max(0, Math.min(100, Number(item.progress) || 0));
    outer.setAttribute("role", "progressbar"); outer.setAttribute("aria-label", "世界内活动进度"); outer.setAttribute("aria-valuenow", progress);
    outer.setAttribute("aria-valuemin", "0"); outer.setAttribute("aria-valuemax", "100"); inner.style.width = `${progress}%`; outer.append(inner); return outer;
  }
  function render() {
    workspace?.render(data, (id, source) => {
      const item = (data.items || []).find(row => row.id === id); if (item) open(item, source);
    });
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
      const simulated = item.steps?.some(step => step.action === "life_simulation");
      const top = el("div", "life-card-top"); top.append(el("span", "people-eyebrow", `${KIND[item.kind] || "生活事项"}${simulated ? " · AI 模拟" : ""}`), el("span", "life-badge", STATE(item)));
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
      data = response; onData?.(data); render();
    } catch (error) { if (revision === request) { grid.replaceChildren(el("div", "life-empty", `读取失败：${error.message}`)); } }
  }
  function localDate(value = new Date()) {
    const date = value instanceof Date ? new Date(value) : new Date(`${value}T12:00:00`);
    if (!Number.isFinite(date.getTime())) return "";
    const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return shifted.toISOString().slice(0, 10);
  }
  function mondayOf(value = new Date()) {
    const day = value instanceof Date ? localDate(value) : String(value);
    const date = new Date(`${day}T12:00:00`);
    if (!Number.isFinite(date.getTime())) return "";
    const offset = (date.getDay() + 6) % 7;
    date.setDate(date.getDate() - offset);
    return localDate(date);
  }
  function shiftDay(day, amount) {
    const date = new Date(`${day || localDate()}T12:00:00`);
    date.setDate(date.getDate() + amount);
    return localDate(date);
  }
  function worldToday() {
    return weekData.today || localDate();
  }
  function shortDay(day) {
    const [, month, date] = String(day || "").split("-");
    return month && date ? `${month}.${date}` : day;
  }
  function renderWeek(response) {
    weekData = response && typeof response === "object" ? response : {};
    const title = $("life-week-title"), caption = $("life-week-caption");
    const note = $("life-week-note"), weekGrid = $("life-week-grid");
    if (!weekGrid) return;
    weekGrid.replaceChildren();
    if (!getSession()) {
      title.textContent = "这一周";
      caption.textContent = "先在顶栏选择一个会话";
      note.textContent = "日历、日程和日记都按当前会话所属的角色与配置范围读取。";
      const empty = el("div", "life-week-empty");
      empty.append(el("span", "", "⌁"), el("h3", "", "还没有可查看的一周"), el("p", "muted", "选择会话后，这里会并排显示七天里的安排、节庆与日记。"));
      weekGrid.append(empty);
      return;
    }
    const days = Array.isArray(weekData.days) ? weekData.days : [];
    title.textContent = weekData.week_start ? `${shortDay(weekData.week_start)} — ${shortDay(weekData.week_end)}` : "这一周";
    const year = Number(String(weekData.week_start || worldToday()).slice(0, 4));
    const calendarButton = $("life-week-generate-calendar");
    if (calendarButton) calendarButton.textContent = `生成 ${year || ""} 年日历`;
    const calendar = weekData.calendar;
    const worldType = calendar?.world_type === "real" ? "现实世界"
      : calendar?.world_type === "fanfiction" ? "同人/原作世界"
        : calendar?.world_type === "original" ? "原创世界" : "世界背景";
    caption.textContent = calendar ? `${worldType} · 日历与生活安排并列，不代表计划都已发生` : "这一周的生活安排与日记";
    note.textContent = calendar
      ? `${worldType}${calendar.culture_notes ? ` · ${calendar.culture_notes}` : ""}`
      : "本年度日历尚未生成。日程与日记仍可查看，也可以点右侧生成本年日历。";
    if (!days.length) {
      const empty = el("div", "life-week-empty");
      empty.append(el("span", "", "⌁"), el("h3", "", "这一周还没有数据"), el("p", "muted", "稍后刷新，或先生成本年日历。"));
      weekGrid.append(empty); return;
    }
    const weekdayLabels = {mon: "周一", tue: "周二", wed: "周三", thu: "周四", fri: "周五", sat: "周六", sun: "周日"};
    const statusLabels = {upcoming: "待开始", past: "已过时", soon: "即将开始", delayed: "时间未定"};
    const sourceLabels = {public_calendar: "公共日历", model: "世界设定", inferred: "推定", manual: "手动"};
    for (const day of days) {
      const card = el("article", `life-week-day${day.is_today ? " is-today" : ""}`);
      const head = el("header", "life-week-day-head");
      const date = el("div");
      date.append(el("strong", "", shortDay(day.date)), el("small", "muted", weekdayLabels[day.weekday] || day.weekday));
      head.append(date);
      if (day.is_today) head.append(el("span", "life-badge", "今天"));
      card.append(head);

      const agenda = el("section", "life-week-section");
      const agendaHead = el("div", "life-week-section-head");
      const sourceLabel = day.agenda_source === "recorded" ? "已记录"
        : day.agenda_source === "today" ? "今天" : "计划";
      agendaHead.append(el("h4", "", "安排"), el("small", "muted", sourceLabel));
      agenda.append(agendaHead);
      if (day.agenda?.length) {
        const list = el("div", "life-week-list");
        for (const item of day.agenda) {
          const row = el("div", `life-week-agenda is-${item.status || "upcoming"}`);
          row.append(el("time", "", item.time || "—"), el("span", "", item.title || "生活安排"),
            el("small", "muted", statusLabels[item.status] || "已安排"));
          list.append(row);
        }
        agenda.append(list);
      } else agenda.append(el("p", "life-week-empty-line", "没有固定安排"));
      card.append(agenda);

      const calendarBox = el("section", "life-week-section");
      calendarBox.append(el("h4", "", "日历与文化"));
      if (day.calendar?.length) {
        const list = el("div", "life-week-list");
        for (const item of day.calendar.slice(0, 4)) {
          const row = el("div", `life-week-event source-${item.source || "inferred"}`);
          row.append(el("strong", "", item.title || "日历事件"),
            el("small", "", sourceLabels[item.source] || item.source || "推定"));
          if (item.description) row.append(el("p", "", String(item.description).slice(0, 120)));
          list.append(row);
        }
        calendarBox.append(list);
      } else calendarBox.append(el("p", "life-week-empty-line", day.calendar_generated ? "今天没有日历事件" : "年历尚未生成"));
      card.append(calendarBox);

      const diary = el("section", "life-week-section life-week-diary");
      diary.append(el("h4", "", "这一天的日记"));
      if (day.diary) {
        diary.append(el("strong", "", day.diary.title || "无题"),
          el("p", "", String(day.diary.content || "").slice(0, 220)),
          el("small", "muted", day.diary.mood || "生活回顾"));
        if (String(day.diary.content || "").length > 220) {
          const more = el("details", "life-week-diary-more");
          more.append(el("summary", "", "继续读这一天 ↓"), el("p", "", String(day.diary.content).slice(220)));
          diary.append(more);
        }
      } else diary.append(el("p", "life-week-empty-line", "还没有日记。有足够生活记录后会自动补写。"));
      card.append(diary);
      weekGrid.append(card);
    }
  }
  async function loadWeek(startDay = "") {
    const revision = ++weekRequest, session = getSession();
    weekSession = session; weekLoading = Boolean(session); weekData = {};
    updateWeekControls();
    $("life-week-title").textContent = "这一周";
    $("life-week-caption").textContent = "正在读取这个会话的安排";
    $("life-week-note").textContent = "";
    const weekGrid = $("life-week-grid");
    if (weekGrid) weekGrid.replaceChildren(el("div", "life-week-empty", "正在整理这一周…"));
    if (!session) { renderWeek({}); return; }
    try {
      const response = await apiGet("life/week", {session, start: startDay || ""});
      if (revision !== weekRequest || session !== getSession()) return;
      renderWeek(response);
    } catch (error) {
      if (revision !== weekRequest || session !== getSession()) return;
      renderWeek({});
      const note = $("life-week-note");
      if (note) note.textContent = `读取失败：${error.message}`;
    } finally {
      if (revision === weekRequest && session === getSession()) {
        weekLoading = false; updateWeekControls();
      }
    }
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
    drawerSession = data.session; drawerScope = getSession(); drawerRevision++;
    selected = item; opener = source; body.replaceChildren(); $("life-drawer-title").textContent = item.text;
    body.append(line("为什么想做", item.motivation), line("接下来", item.next_step), bar(item));
    if (item.obstacle) body.append(line("阻碍", item.obstacle));
    if (item.recovery_condition) body.append(line("怎样继续", item.recovery_condition));
    if (item.resume_at) body.append(line("适合继续的时间", TIME(item.resume_at)));
    if (item.origin_event || item.event_links?.length) body.append(line("从经历里留下的牵挂", (data.experiences || []).filter(row => row.id === item.origin_event || item.event_links?.includes(row.id)).map(row => row.title).join("、") || "关联经历保存在生活记录中"));
    if (item.result) body.append(line("留下的结果", item.result));
    const steps = el("ol", "life-steps");
    for (const step of item.steps || []) {
      const row = el("li", step.status === "done" ? "done" : ""); row.append(el("b", "", step.text), el("small", "muted", `${step.action === "life_simulation" ? "AI 模拟" : "世界内活动"} · ${step.status === "done" ? TIME(step.finished_at) : `安排中 · ${step.minutes} 分钟`}`)); steps.append(row);
    }
    body.append(line("世界内的生活", "编程、创作等个人活动由 AI 模拟，不操作真实电脑或文件；对现实用户的约定仍需实际兑现。"));
    if (steps.children.length) body.append(line("一件事，分成几小步", "进度按已进行的世界内活动计算。"), steps);
    const unfinished = (item.steps || []).find(step => step.status !== "done" && (!step.goal || step.goal === item.text));
    if (!CLOSED.has(item.status) && unfinished?.worked_minutes > 0) {
      const saved = unfinished.bookmark && (!unfinished.bookmark.goal || unfinished.bookmark.goal === item.text) ? unfinished.bookmark : {};
      const openQuestion = saved.open_question !== undefined ? saved.open_question : unfinished.open_question;
      const bookmark = el("section", "life-stage-note life-bookmark");
      bookmark.append(el("small", "people-eyebrow", "中途书签 · 尚未完成"),
        el("h3", "", unfinished.text),
        el("p", "muted", `已投入 ${unfinished.worked_minutes} 分钟 · 还需约 ${Math.max(0, unfinished.minutes - unfinished.worked_minutes)} 分钟`));
      if (saved.note) bookmark.append(el("p", "life-stage-content", saved.note));
      bookmark.append(line("下一处", saved.next_focus || unfinished.focus || unfinished.text));
      if (openQuestion) bookmark.append(line("还没解开的地方", openQuestion));
      bookmark.append(el("small", "muted", "世界内的中途记录；接续方向不代表已经取得成果。"));
      body.append(bookmark);
    }
    for (const stage of [...(item.stage_notes || [])].reverse()) {
      const note = el("section", "life-stage-note");
      note.append(el("small", "people-eyebrow", `阶段笔记 · 世界内模拟 · ${TIME(stage.at)}`),
        el("h3", "", stage.summary), el("p", "life-stage-content", stage.content));
      if (stage.goal && stage.goal !== item.text) note.append(line("当时的目标", stage.goal));
      if (stage.next_focus) note.append(line("接下来从这里继续", stage.next_focus));
      body.append(note);
    }
    const timeline = el("div", "life-timeline");
    for (const event of [...(item.history || [])].reverse()) {
      const row = el("div", "life-timeline-item"); row.append(el("time", "", TIME(event.at)), el("p", "", event.text));
      if (event.receipt) row.append(el("small", "muted", event.receipt.kind === "life_simulation" ? `AI 模拟 · ${event.receipt.minutes || 0} 分钟` : event.receipt.kind === "in_world_action" ? `世界内活动 · ${event.receipt.minutes || 0} 分钟` : "已保存兑现依据"));
      if (event.manual) row.append(el("small", "muted", "管理员调整")); timeline.append(row);
    }
    body.append(line("这件事怎样走到了现在", ""), timeline);
    const actions = el("div", "life-drawer-actions");
    if (!CLOSED.has(item.status)) {
      actions.append(button(item.status === "paused" ? "继续这件事" : "暂时放下", () => mutate(item.status === "paused" ? "resume" : "pause")),
        button("晚一点继续", () => defer(item)), button("安排时间…", () => appointment(item)), button("编辑", () => form(item)), button("确认结果…", () => outcome(item)));
    }
    actions.append(button("相关模型调用 ↗", () => { drawer.close(); openCalls(item.id, drawerSession); }), button("相关记忆 ↗", () => { drawer.close(); openMemories(item.text, drawerScope); }));
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
    body.append(el("p", "muted", "她会在清醒、有精力且空闲时，安排世界内活动或 AI 模拟的小步。对现实用户的约定会等待实际兑现依据。"),
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
  function appointment(item) {
    drawerRevision++; body.replaceChildren(); $("life-drawer-title").textContent = "给这件牵挂留一点时间";
    const date = field("哪一天", data.day || new Date().toLocaleDateString("sv-SE"), "date"), time = field("几点开始", "20:00", "time");
    body.append(line("接着做", item.text), date.wrap, time.wrap, el("p", "muted", "日程到点时接续同一件事项，共用已有进度。困了、遇上重要事件或条件不足时可以暂缓。"),
      button("保存到日程", () => { if (!date.input.value || !time.input.value) { toast("请填写日期和时间"); return; } mutate("appointment", {date: date.input.value, time: time.input.value}); }, "primary"));
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
  $("life-settings").addEventListener("click", () => { $("life-tools").removeAttribute("open"); openSettings(); });
  $("life-browse-settings").addEventListener("click", () => openBrowseSettings());
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
  $("life-week-prev").addEventListener("click", () => {
    loadWeek(shiftDay(weekData.week_start || mondayOf(), -7));
  });
  $("life-week-today").addEventListener("click", () => loadWeek());
  $("life-week-next").addEventListener("click", () => {
    loadWeek(shiftDay(weekData.week_start || mondayOf(), 7));
  });
  $("life-week-generate-calendar").addEventListener("click", async () => {
    const session = getSession();
    if (!session) { toast("先选择一个会话"); return; }
    if (weekLoading || weekSession !== session || !weekData.week_start || weekPending.has("calendar")) return;
    const revision = weekRequest, start = weekData.week_start;
    const current = () => session === getSession() && revision === weekRequest;
    const year = Number(start.slice(0, 4));
    weekPending.add("calendar"); updateWeekControls();
    try {
      const response = await apiPost("calendar/generate", {session, year, force: true, extra_prompt: generationHint.value.trim()});
      if (!current()) return;
      if (!response.ok) throw new Error(response.error || response.message || "日历生成失败");
      toast(`${year} 年日历已生成`);
      await loadWeek(start);
    } catch (error) {
      if (current()) toast(error.message || String(error));
    } finally {
      weekPending.delete("calendar"); updateWeekControls();
    }
  });
  $("life-week-generate-diary").addEventListener("click", async () => {
    const session = getSession();
    if (!session) { toast("先选择一个会话"); return; }
    if (weekLoading || weekSession !== session || !weekData.today || weekPending.has("diary")) return;
    const revision = weekRequest, start = weekData.week_start, day = shiftDay(weekData.today, -1);
    const current = () => session === getSession() && revision === weekRequest;
    weekPending.add("diary"); updateWeekControls();
    try {
      const response = await apiPost("diary/generate", {session, day, extra_prompt: generationHint.value.trim()});
      if (!current()) return;
      if (!response.ok) throw new Error(response.error || response.message || "日记生成失败");
      toast(`${day} 的日记已补写`);
      await loadWeek(start);
    } catch (error) {
      if (current()) toast(error.message || String(error));
    } finally {
      weekPending.delete("diary"); updateWeekControls();
    }
  });
  $("life-filter").addEventListener("change", event => { filter = event.target.value; render(); });
  $("life-sort").addEventListener("change", event => { sort = event.target.value; render(); });
  $("life-search").addEventListener("input", render);
  return {reload, loadWeek, select: view => workspace?.select(view), openSchedules: () => workspace?.openSchedules()};
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

export function openLifeSettings({world, el, openCustomDialog, markDirty, isCurrent = () => true}) {
  const limits = {...world.limits};
  const controls = [];
  openCustomDialog({title: "生活的节奏与后台预算", confirmText: "应用设置", hint: "应用后使用顶栏「保存」写入配置。0 表示不设对应预算上限。", build: host => {
    const body = el("div", "life-budget-settings"); host.append(body);
    for (const [key, label, fallback, min, max] of [
      ["life_matter_active_max", "活跃事项上限", 6, 1, 20], ["life_matter_daily_minutes", "每件事每天最多投入（分钟）", 90, 5, 480],
      ["life_matter_interval_minutes", "再次推进前的间隔（分钟）", 60, 5, 1440], ["background_daily_calls", "每天后台调用预算", 1000, 0, 10000],
      ["background_daily_tokens", "每天后台 token 预算", 0, 0, 100000000], ["background_concurrency", "同时运行的后台模型请求", 1, 1, 12]]) {
      const wrap = el("label", "field"), input = el("input"); input.type = "number"; input.min = min; input.max = max; input.value = limits[key] ?? fallback;
      wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { if (!input.checkValidity()) throw new Error(`${label}超出范围`); limits[key] = Number(input.value); });
    }
    for (const [object, key, label, fallback] of [[limits, "life_matter_enabled", "自动推进生活事项", true], [limits, "life_matter_self_start", "空闲时会自己产生个人打算", true]]) {
      const wrap = el("label", "field memory-toggle"), input = el("input"); input.type = "checkbox"; input.checked = object[key] ?? fallback; wrap.append(el("span", "", label), input); body.append(wrap); controls.push(() => { object[key] = input.checked; });
    }
    body.append(el("p", "muted", "Token 缺失时保留保守的预算占位，界面仍标为未知。记忆与模型检索请在「设置 → 记忆与资料」调整。"));
    body.append(el("p", "muted", "生活事项在角色世界内模拟推进，无需电脑环境。日程、睡眠、精力和后台预算共同决定她何时继续。"));
  }, onSubmit: () => { if (!isCurrent()) throw new Error("配置范围已改变，请重新打开设置"); controls.forEach(read => read()); world.limits = limits; markDirty(); return true; }});
}
