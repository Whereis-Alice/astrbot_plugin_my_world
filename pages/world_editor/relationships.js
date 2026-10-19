import { personAvatar, personColor, socialRange, distancePreview, bindPeopleSort } from "./people-ui.js";
import { renderRelationshipOverview, renderRelationshipHistory, describeMiss } from "./contact-relationship.js";

/** One identity, many independent conversations. No model calls or writes on read. */
export function createRelationshipsPage({ root, el, apiGet, apiPost, toast,
  formatStamp, confirm, openRules, openContact }) {
  const $ = id => root.querySelector(`#${id}`);
  let data = null, person = null, revision = 0, detailRevision = 0;
  let page = 1, filter = "all", sort = "recent", searchTimer, busy = false, dirty = false, opener = null;
  const grid = $("relationships-grid"), drawer = $("relationship-drawer");
  const body = $("relationship-body"), scrim = $("relationship-scrim");
  const globalGet = (url, params = {}) => apiGet(url, { ...params, config_scope: "" });
  const globalPost = (url, params = {}) => apiPost(url, { ...params, config_scope: "" });

  function action(text, fn, className = "ghost small") {
    const button = el("button", className, text);
    button.type = "button";
    button.addEventListener("click", fn);
    return button;
  }
  function empty(box, text, retry) {
    const state = el("div", "people-empty");
    state.setAttribute("role", "status");
    state.append(el("span", "people-empty-symbol", "◎"), el("p", "", text));
    if (retry) state.append(action("重新读取", retry));
    box.replaceChildren(state);
  }
  function chips(names, className = "social-chips") {
    const box = el("div", className);
    names.forEach(name => box.append(el("span", "social-chip", name)));
    if (!names.length) box.append(el("span", "people-muted", "未设置正式身份"));
    return box;
  }
  function renderCards() {
    grid.replaceChildren();
    $("relationships-count").textContent = `${data.people_total || 0} 位联系人`;
    $("relationships-summary").textContent = `${data.baselines_total || 0} 位已设起点 · ${data.sessions_total || 0} 个会话独立发展`;
    if (!data.people?.length) {
      empty(grid, $("relationships-search").value || filter !== "all"
        ? "没有找到符合条件的人。试试 QQ 号或其他称呼。"
        : "从一个 QQ 号开始，为她与这个人的相处设定起点。", () => editNew());
    }
    for (const item of data.people || []) {
      const card = el("button", "social-card");
      card.type = "button";
      card.dataset.qq = item.user_id;
      card.dataset.startingPoint = String(Boolean(item.baseline));
      card.style.setProperty("--person-color", personColor(item.user_id));
      const head = el("span", "people-card-head");
      const identity = el("span", "people-card-identity");
      identity.append(el("strong", "people-card-name", item.name), el("span", "people-qq", `QQ ${item.user_id}`));
      head.append(personAvatar(el, item.user_id, item.name), identity,
        el("span", `social-status ${item.baseline ? "is-set" : ""}`, item.baseline ? "已设起点" : "自然发展"));
      const score = el("span", "social-score");
      score.append(el("span", "people-eyebrow", item.baseline ? "统一起点好感" : "各会话好感"),
        el("strong", "", item.baseline ? String(item.baseline.affinity) : socialRange(item.sessions)));
      const track = el("span", "social-affinity-track");
      track.setAttribute("aria-hidden", "true");
      const values = item.baseline ? [Number(item.baseline.affinity)] : item.sessions.map(scope => Number(scope.affinity));
      const minimum = Number(data.rules?.affinity_min ?? -100), maximum = Number(data.rules?.affinity_max ?? 100);
      const position = value => `${Math.max(0, Math.min(100, (value - minimum) / Math.max(1, maximum - minimum) * 100))}%`;
      track.style.setProperty("--neutral-position", position(0));
      for (const value of values.filter(Number.isFinite).slice(0, 12)) {
        const mark = el("i"); mark.style.left = position(value); track.append(mark);
      }
      score.append(track);
      const names = item.baseline?.relations || [...new Set(item.sessions.flatMap(s => s.relations))];
      const identities = chips(names.slice(0, 4));
      if (names.length > 4) identities.append(el("span", "social-chip", `+${names.length - 4}`));
      const sessions = el("span", "social-card-sessions");
      const changed = item.baseline && item.sessions.some(s => Number(s.affinity) !== Number(item.baseline.affinity)
        || [...s.relations].sort().join("|") !== [...item.baseline.relations].sort().join("|"));
      sessions.append(el("span", "social-card-scope", `${item.sessions.length} 个会话${changed ? " · 已有独立变化" : ""}`));
      if (sort.startsWith("affinity")) {
        const scores = item.sessions.map(scope => Number(scope.affinity)).filter(Number.isFinite);
        const average = scores.length ? scores.reduce((sum, value) => sum + value, 0) / scores.length : Number(item.baseline?.affinity || 0);
        sessions.append(el("span", "social-sort-value", `${scores.length ? "平均好感" : "起点好感"} · ${Number(average.toFixed(2))}`));
      }
      for (const scope of item.sessions.slice(0, 2)) {
        const row = el("span", "social-session-line");
        row.append(el("span", "", scope.label), el("b", "", String(Number(Number(scope.affinity).toFixed(1)))));
        sessions.append(row);
      }
      if (!item.sessions.length) sessions.append(el("span", "people-muted", "下次相遇时，各会话会从这里开始。"));
      const foot = el("span", "people-card-foot");
      foot.append(el("span", "", item.baseline ? `设定于 ${formatStamp(item.baseline.updated_at)}` : "尚未设定统一起点"),
        el("b", "", "查看与调整 ↗"));
      card.append(head, score, identities, sessions, foot);
      card.addEventListener("click", () => select(item.user_id, card));
      grid.append(card);
    }
    $("relationships-page").textContent = `${data.page || 1} / ${data.pages || 1}`;
    $("relationships-prev").disabled = data.page <= 1;
    $("relationships-next").disabled = data.page >= data.pages;
    $("relationships-pagination").hidden = (data.pages || 1) <= 1;
  }
  async function reload() {
    const request = ++revision;
    grid.setAttribute("aria-busy", "true");
    if (!data) empty(grid, "正在整理她认识的人…");
    try {
      const result = await globalGet("relationships", { q: $("relationships-search").value.trim(), page, filter, sort });
      if (request !== revision) return;
      data = result;
      page = result.page || 1;
      renderCards();
    } catch (error) {
      if (request === revision) empty(grid, `读取失败：${error.message || error}`, reload);
    } finally {
      if (request === revision) grid.removeAttribute("aria-busy");
    }
  }
  function show(openerElement) {
    opener = openerElement || document.activeElement;
    drawer.hidden = false;
    scrim.hidden = false;
    root.classList.add("social-open");
    $("relationship-close").focus();
  }
  async function close() {
    if (busy) return;
    if (dirty && !await confirm({ title: "放弃尚未应用的修改？", message: "各会话还没有被修改。", confirmText: "放弃修改" })) return;
    ++detailRevision;
    drawer.hidden = true;
    scrim.hidden = true;
    root.classList.remove("social-open");
    dirty = false;
    const currentCard = person && grid.querySelector(`[data-qq="${CSS.escape(String(person.user_id))}"]`);
    (currentCard || opener)?.focus();
  }
  async function select(uid, trigger) {
    const request = ++detailRevision;
    person = null;
    dirty = false;
    show(trigger);
    empty(body, "正在读取统一起点与各会话现状…");
    $("relationship-title").textContent = `QQ ${uid}`;
    try {
      const result = await globalGet("relationships/person", { user_id: uid });
      if (request !== detailRevision) return;
      person = result;
      renderPerson();
    } catch (error) {
      if (request === detailRevision) empty(body, `读取失败：${error.message || error}`, () => select(uid));
    }
  }
  function editor({ affinity, relations, rules, baseline, onSave }) {
    let names = [...relations], value = Number(affinity);
    const form = el("form", "social-editor");
    const mark = () => { dirty = true; form.dataset.touched = "true"; };
    const score = el("div", "social-edit-score");
    const numberLabel = el("label", "social-number-label", "好感度");
    const number = el("input", "social-number");
    number.type = "number";
    number.min = rules.affinity_min ?? -100;
    number.max = rules.affinity_max ?? 100;
    number.step = "any";
    number.value = String(value);
    number.required = true;
    number.setAttribute("aria-label", baseline ? "统一起点好感" : "此会话好感");
    numberLabel.append(number);
    const distance = el("span", "social-distance-preview");
    score.append(numberLabel, distance);
    const range = el("input", "social-slider");
    range.type = "range";
    range.min = number.min; range.max = number.max; range.step = "0.1"; range.value = number.value;
    range.setAttribute("aria-label", "拖动调整好感");
    const limits = el("div", "social-scale");
    limits.append(el("span", "", range.min), el("span", "", "中性"), el("span", "", range.max));
    const group = el("fieldset", "social-identity-picker");
    group.append(el("legend", "", "关系身份"), el("p", "people-muted", "可选择多个能并存的身份。同一替代组或槽位只保留一种。"));
    const picker = el("div", "social-picker-options");
    const explanation = el("p", "social-distance-reason");
    distance.setAttribute("aria-live", "polite");
    const sync = () => {
      if (!Number.isFinite(value) || value < Number(number.min) || value > Number(number.max)) {
        distance.textContent = "等待有效好感值";
        explanation.textContent = `请输入 ${number.min} 至 ${number.max} 之间的好感度。`;
        return;
      }
      const preview = distancePreview(rules, value, names);
      distance.textContent = `预计相处档位 · ${preview.name}`;
      explanation.textContent = preview.reason;
    };
    const render = () => {
      picker.replaceChildren();
      for (const bond of rules.bonds || []) {
        const selected = names.includes(bond.name);
        const button = action(bond.name, () => {
          if (selected) names = names.filter(name => name !== bond.name);
          else {
            names = names.filter(name => {
              const old = rules.bonds.find(item => item.name === name);
              return !(bond.group && bond.group === old?.group) && !(bond.slot && bond.slot === old?.slot)
                && name !== rules.default_bond && bond.name !== rules.default_bond;
            });
            names.push(bond.name);
          }
          mark(); render(); sync();
        }, `social-choice ${selected ? "selected" : ""}`);
        button.setAttribute("aria-pressed", String(selected));
        button.title = `${bond.unique ? "唯一身份 · " : ""}${bond.auto_form ? "允许自然形成" : "自然形成关闭"}`;
        if (selected) button.append(el("span", "", " ✓"));
        picker.append(button);
      }
    };
    render(); sync();
    group.append(picker);
    const error = el("p", "social-error");
    error.setAttribute("role", "alert");
    const save = el("button", "primary social-apply", baseline ? "保存并应用到所有会话" : "保存此会话");
    save.type = "submit";
    const onNumber = () => { value = number.valueAsNumber; if (Number.isFinite(value)) range.value = number.value; mark(); sync(); };
    number.addEventListener("input", onNumber);
    range.addEventListener("input", () => { number.value = range.value; onNumber(); });
    form.append(score, range, limits, explanation, group);
    if (baseline) form.append(el("p", "social-apply-note", "保存后，所有现有会话的当前好感与关系会被替换为以上设定；新会话也以此起步。之后各自发展，经历与历史记录保留。"));
    form.append(error, save);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy || !form.reportValidity()) return;
      const otherDrafts = [...body.querySelectorAll('form[data-touched="true"]')].some(node => node !== form);
      if (otherDrafts && !await confirm({title: "保存并放弃其他未保存修改？",
        message: "这里只提交当前表单，页面刷新后其他表单的草稿将丢弃。", confirmText: "保存当前表单"})) return;
      busy = true; save.disabled = true; error.textContent = "";
      try {
        await onSave({ affinity: Number(number.value), relations: names });
        dirty = false;
      } catch (problem) {
        error.textContent = problem.message || String(problem);
      } finally { busy = false; save.disabled = false; }
    });
    return form;
  }
  function renderPerson() {
    const current = person;
    body.replaceChildren();
    $("relationship-title").textContent = current.name;
    $("relationship-subtitle").textContent = `QQ ${current.user_id} · 一个统一起点，各会话独立相处`;
    const identity = el("div", "social-detail-identity");
    identity.append(personAvatar(el, current.user_id, current.name), el("span", "people-qq", `QQ ${current.user_id}`));
    const start = el("section", "social-start-section");
    const head = el("div", "people-section-head");
    head.append(el("h3", "", "统一起点"), el("span", "social-status is-set", current.baseline ? "已设定" : "尚未设置"));
    start.append(head);
    const nameLabel = el("label", "social-name-label", "展示称呼");
    const nameInput = el("input");
    nameInput.value = current.baseline?.name || (current.name === current.user_id ? "" : current.name);
    nameInput.placeholder = "方便你辨认，不会覆盖各会话里的称呼";
    nameInput.maxLength = 80;
    nameInput.addEventListener("input", () => { dirty = true; start.querySelector("form").dataset.touched = "true"; });
    nameLabel.append(nameInput);
    start.append(nameLabel);
    const rules = current.rules || data.rules;
    start.append(editor({ affinity: current.baseline?.affinity ?? rules.affinity_initial ?? 0,
      relations: current.baseline?.relations || [], rules, baseline: true,
      onSave: async values => {
        const result = await globalPost("relationships/apply", { user_id: current.user_id,
          name: nameInput.value.trim(), revision: current.baseline?.revision || 0, ...values });
        toast(`已应用到 ${result.applied_sessions} 个会话，之后各自独立发展`);
        await refreshPerson(current.user_id);
      } }));
    if (current.baseline) start.append(action("移除统一起点…", async () => {
      if (busy || !await confirm({title: "移除这个人的统一起点？", message: "现有会话的好感与关系保留。以后新认识他的会话使用普通初始值。", confirmText: "移除起点"})) return;
      try {
        await globalPost("relationships/baseline", { user_id: current.user_id, revision: current.baseline.revision });
        dirty = false; await refreshPerson(current.user_id); toast("已移除起点，现有会话保持原样");
      } catch (error) { toast(error.message || "移除失败"); }
    }, "social-remove ghost small"));
    const scopes = el("section", "social-scopes-section");
    const scopeHead = el("div", "people-section-head");
    scopeHead.append(el("h3", "", "各会话现状"), el("span", "people-muted", `${current.sessions.length} 份独立记录`));
    scopes.append(scopeHead);
    if (!current.sessions.length) scopes.append(el("p", "people-muted", "还没有会话档案。保存起点后会为已配置会话分别建立记录。"));
    for (const scope of current.sessions) {
      const card = el("details", "social-scope-card");
      card.dataset.session = scope.session;
      const summary = el("summary");
      const text = el("span", "social-scope-name");
      text.append(el("strong", "", scope.label), el("span", "people-qq", scope.session));
      summary.append(text, el("span", "social-scope-score", String(scope.affinity)), el("span", "social-chip", scope.level), el("span", "social-disclose", "+"));
      card.append(summary, chips(scope.relations));
      const content = el("div", "social-scope-content");
      if (scope.issue) content.append(el("p", "social-error", scope.issue));
      if (scope.configured) content.append(editor({affinity: scope.affinity, relations: scope.relations,
        rules: scope.rules || rules, baseline: false, onSave: async values => {
          await globalPost("relationships/session", { user_id: current.user_id, session: scope.session, ...values });
          toast("已保存此会话，其他会话与统一起点保持原样"); await refreshPerson(current.user_id);
        }}));
      else content.append(el("p", "people-muted", "此会话已移出配置，保留历史供查看。"));
      content.append(action("查看关系依据与变化记录", () => loadEvidence(current.user_id, scope.session, content)));
      content.append(action("打开此会话的联系人档案 ↗", async () => {
        if (dirty && !await confirm({title: "放弃尚未应用的修改？", message: "将打开此人的通讯录档案。", confirmText: "打开档案"})) return;
        dirty = false; await close(); openContact(current.user_id, scope.session);
      }));
      card.append(content); scopes.append(card);
    }
    const layout = el("div", "social-detail-layout");
    layout.append(start, scopes);
    body.append(identity, layout);
  }
  async function loadEvidence(uid, session, container) {
    let target = container.querySelector(".social-evidence");
    if (target) { target.hidden = !target.hidden; return; }
    const request = detailRevision;
    target = el("div", "social-evidence");
    container.append(target);
    empty(target, "正在读取原话与变化…");
    try {
      const detail = await apiGet("profile/contact", { session, user_id: uid, config_scope: "" });
      if (request !== detailRevision || !target.isConnected) return;
      target.replaceChildren();
      const overview = renderRelationshipOverview(detail, {el, formatStamp});
      if (overview) target.append(overview);
      target.append(el("p", "people-muted", describeMiss(detail.miss || {}, detail.miss_threshold || .7)));
      const candidates = (detail.bonds || []).filter(bond => bond.status === "claimed");
      for (const bond of candidates) {
        const row = el("div", "social-candidate");
        row.append(el("span", "", `待确认 · ${bond.type}`));
        for (const [label, act] of [["认下", "accept"], ["删除候选", "delete"]]) row.append(action(label, async () => {
          if (busy) return;
          if (dirty && !await confirm({title: "处理候选并放弃未保存修改？", message: "处理后会重新读取各会话现状。", confirmText: "继续处理"})) return;
          busy = true;
          try {
            await apiPost("profile/bond", { session, user_id: uid, action: act, id: bond.id, policy: "reject", config_scope: "" });
            await refreshPerson(uid); toast("关系记录已更新");
          } catch (error) { toast(error.message || "操作失败"); }
          finally {busy = false;}
        }));
        target.append(row);
      }
      const history = renderRelationshipHistory(detail, {el, formatStamp});
      if (history) { history.open = true; target.append(history); }
      const grudge = el("section", "social-grudge");
      grudge.append(el("h4", "", "心里还记着的事"));
      const active = detail.grudge;
      grudge.append(el("p", "people-muted", active?.reason || active?.text || "目前没有放不下的事。"));
      if (active?.until_text) grudge.append(el("p", "people-muted", `预计淡去：${active.until_text}`));
      grudge.append(el("p", "people-muted", "情绪属于她的生活状态；共享生活组内也会共同感受到。关系与好感数值仍各会话独立。"));
      const reason = el("input"); reason.placeholder = "写一句她还在意的事";
      reason.setAttribute("aria-label", "她还在意的事"); reason.maxLength = 300;
      const controls = el("div", "social-candidate");
      const error = el("p", "social-error"); error.setAttribute("role", "alert");
      const update = async act => {
        if (busy) return;
        if (act === "add" && !reason.value.trim()) { error.textContent = "先写一句她还在意的事"; return; }
        busy = true; error.textContent = "";
        try {
          await apiPost("profile/grudge", {session, user_id: uid, action: act, reason: reason.value.trim(), config_scope: ""});
          target.remove(); await loadEvidence(uid, session, container); toast("已更新她记着的事");
        } catch (problem) {error.textContent = problem.message || String(problem);}
        finally {busy = false;}
      };
      controls.append(action(active ? "更新这件事" : "记下一件事", () => update("add")));
      if (active) controls.append(action("已经释怀", () => update("resolve")), action("删除记录", () => update("delete")));
      grudge.append(reason, controls, error); target.append(grudge);
    } catch (error) { empty(target, `读取失败：${error.message || error}`, () => { target.remove(); loadEvidence(uid, session, container); }); }
  }
  async function refreshPerson(uid) {
    const request = ++detailRevision;
    const fresh = await globalGet("relationships/person", {user_id: uid});
    if (request !== detailRevision || drawer.hidden) return;
    person = fresh; dirty = false; renderPerson(); await reload();
  }
  function editNew() {
    person = {user_id: "", name: "新联系人", baseline: null, sessions: [], rules: data?.rules || {}};
    dirty = false; ++detailRevision; show();
    $("relationship-title").textContent = "从一个 QQ 号开始";
    $("relationship-subtitle").textContent = "先找到这个人，再设定好感与关系";
    const form = el("form", "social-new-person");
    const label = el("label", "", "QQ 号");
    const input = el("input");
    input.inputMode = "numeric"; input.pattern = "[1-9][0-9]{0,19}"; input.required = true;
    input.placeholder = "输入 QQ 号"; input.setAttribute("aria-label", "QQ 号");
    label.append(input);
    const button = el("button", "primary", "继续设置 →"); button.type = "submit";
    form.append(label, el("p", "people-muted", "即使还没和她聊过，也可以提前设定。QQ 号相同的联系人会归到同一张卡片。"), button);
    form.addEventListener("submit", event => { event.preventDefault(); if (form.reportValidity()) select(input.value.trim()); });
    body.replaceChildren(form); input.focus();
  }
  sort = bindPeopleSort($("relationships-sort"), [
    ["recent", "最近互动"], ["affinity", "好感 · 从高到低"], ["affinity_asc", "好感 · 从低到高"],
    ["sessions", "会话数量 · 从多到少"], ["name", "名称顺序"],
  ], value => { sort = value; page = 1; reload(); }, "好感按各会话当前值的平均数排序；尚无会话时使用统一起点。最近互动按最后联系时间排序，未交谈的人排在末尾。");
  $("relationships-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => {page = 1; reload();}, 250); });
  root.querySelectorAll("[data-relationship-filter]").forEach(button => button.addEventListener("click", () => {
    filter = button.dataset.relationshipFilter; page = 1;
    root.querySelectorAll("[data-relationship-filter]").forEach(item => {
      item.classList.toggle("on", item === button); item.setAttribute("aria-pressed", String(item === button));
    });
    reload();
  }));
  $("relationships-refresh").addEventListener("click", reload);
  $("relationships-add").addEventListener("click", editNew);
  $("relationships-rules").addEventListener("click", openRules);
  $("relationship-close").addEventListener("click", close);
  scrim.addEventListener("click", close);
  $("relationships-prev").addEventListener("click", () => {page--; reload();});
  $("relationships-next").addEventListener("click", () => {page++; reload();});
  drawer.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.getElementById("dialog").classList.contains("hidden")) { event.preventDefault(); close(); }
    if (event.key === "Tab") {
      const nodes = [...drawer.querySelectorAll('button:not(:disabled), input, select, textarea, summary, [tabindex="0"]')].filter(node => node.getClientRects().length);
      const first = nodes[0], last = nodes.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  return {reload, select, close};
}
