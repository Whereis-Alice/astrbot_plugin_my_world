/** Scoped learning drawers. Async results and writes stay bound to the opened conversation. */
export function createSocialLearning({apiGet, apiPost, el, toast, sessions = () => [], resolveSession = value => value}) {
  let revision = 0, dialog;
  const time = value => new Date(value * 1000).toLocaleString("zh-CN", {hour12: false});
  function open(title, session, render, trace = "") {
    session = resolveSession(session);
    document.querySelectorAll("details.command-menu[open]").forEach(menu => menu.removeAttribute("open"));
    revision++;
    dialog?.close(); dialog?.remove();
    const current = dialog = el("dialog", "learning-drawer");
    const head = el("header", "learning-head");
    const titleNode = el("h2", "", title); titleNode.id = "learning-title";
    current.setAttribute("aria-labelledby", titleNode.id);
    const close = el("button", "ghost", "关闭 ×"); close.type = "button";
    close.onclick = () => current.close();
    head.append(titleNode, close);
    const select = el("select"); select.setAttribute("aria-label", "学习记录所属会话");
    const actual = sessions();
    if (actual.length) {
      for (const item of actual) {
        const option = el("option", "", item.label); option.value = item.value; select.append(option);
      }
    } else {
      const choices = document.querySelector("#contacts-session") || document.querySelector("#memory-session");
      for (const option of choices?.options || []) if (option.value) select.append(option.cloneNode(true));
    }
    if (session && ![...select.options].some(option => option.value === session)) {
      const option = el("option", "", session); option.value = session; select.append(option);
    }
    select.value = session || select.options[0]?.value || "";
    const body = el("div", "learning-body");
    current.append(head, select, body); document.body.append(current);
    current.addEventListener("close", () => { if (dialog === current) revision++; });
    async function load() {
      const ticket = ++revision, selected = select.value;
      body.replaceChildren(el("p", "muted", "正在读取…"));
      if (!selected) { body.replaceChildren(el("p", "muted", "请先添加并选择一个会话。")); return; }
      const active = () => revision === ticket && current.open && select.value === selected;
      try { await render({body, session: selected, trace: selected === session ? trace : "", active, reload: load}); }
      catch (error) { if (active()) body.replaceChildren(el("p", "learning-error", error.message || "读取失败，请重试")); }
    }
    select.onchange = load;
    current.showModal(); load();
    return current;
  }
  function card(title, text, meta = "") {
    const box = el("article", "learning-card");
    box.append(el("strong", "", title), el("p", "", text));
    if (meta) box.append(el("small", "muted", meta));
    return box;
  }
  async function write(session, payload, active, reload) {
    try { await apiPost("social-learning/update", {session, ...payload}); if (active()) { toast("已保存"); await reload(); } }
    catch (error) { if (active()) toast(error.message || "保存失败"); }
  }
  function openVocabulary(session) {
    return open("会话词典", session, async ({body, session, active, reload}) => {
      const data = await apiGet("social-learning", {session}); if (!active()) return;
      body.replaceChildren(el("p", "muted", "只在当前对话出现对应词语时提供解释。修正和停用只影响所选会话。"));
      const form = el("form", "learning-form"), term = el("input"), description = el("input");
      term.placeholder = "词语"; term.maxLength = 32; term.required = true; term.setAttribute("aria-label", "词语");
      description.placeholder = "在这个会话里的含义"; description.maxLength = 160; description.required = true;
      description.setAttribute("aria-label", "词语含义");
      const submit = el("button", "primary", "保存词条"); submit.type = "submit";
      form.append(term, description, submit);
      form.onsubmit = async event => { event.preventDefault(); submit.disabled = true;
        await write(session, {operation: "vocabulary", term: term.value, description: description.value}, active, reload);
        if (active()) submit.disabled = false;
      };
      body.append(form);
      for (const item of data.vocabulary || []) {
        const box = card(item.term, item.description, `${item.manual ? "管理员维护" : "来自明确解释"} · ${item.status === "disabled" ? "已停用" : "使用中"}`);
        const actions = el("div", "learning-actions"), edit = el("button", "ghost small", "修改"), toggle = el("button", "ghost small", item.status === "disabled" ? "启用" : "停用");
        edit.onclick = () => { term.value = item.term; description.value = item.description; term.focus(); };
        toggle.onclick = async () => { toggle.disabled = true; await write(session, {operation: "vocabulary", term: item.term,
          description: item.description, status: item.status === "disabled" ? "active" : "disabled"}, active, reload); if (active()) toggle.disabled = false; };
        actions.append(edit, toggle); box.append(actions);
        if (item.evidence?.length) {
          const details = el("details"), summary = el("summary", "", "查看原话来源"); details.append(summary);
          for (const source of item.evidence) details.append(el("p", "learning-quote", `${source.name || source.user_id || "用户"}：${source.text}`));
          box.append(details);
        }
        body.append(box);
      }
      if (!data.vocabulary?.length) body.append(card("共同语言会慢慢积累", "用户明确解释过的群梗、简称和特别叫法，会记录在这里。也可以手动添加。"));
    });
  }
  function openPerson(session, user) {
    const current = open("相处习惯与互动依据", session, async ({body, session, active, reload}) => {
      const data = await apiGet("social-learning", {session, user_id: user}); if (!active()) return;
      const detail = data.interaction;
      body.replaceChildren(el("p", "muted", "这些经验服从原有人设及关系权限。手动保存后暂停自动归纳；好感和关系身份不受本页操作影响。"));
      const form = el("form", "learning-form"), text = el("textarea");
      text.value = (detail.habits || []).map(item => item.text).join("\n"); text.rows = 3;
      text.placeholder = "每行一条，最多三条；留空保存可清除并暂停自动归纳";
      text.setAttribute("aria-label", "相处习惯");
      const save = el("button", "primary", "保存并由我维护"), resume = el("button", "ghost", "恢复自动归纳");
      save.type = "submit"; resume.type = "button";
      form.append(text, save, resume); body.append(form);
      form.onsubmit = async event => { event.preventDefault(); save.disabled = true; await write(session,
        {operation: "habits", user_id: user, revision: detail.revision, text: text.value}, active, reload); if (active()) save.disabled = false; };
      resume.disabled = !detail.manual;
      resume.onclick = async () => { resume.disabled = true; await write(session,
        {operation: "resume", user_id: user, revision: detail.revision}, active, reload); if (active()) resume.disabled = !detail.manual; };
      const describe = value => Array.isArray(value) ? value.map(row => row.text || "").join("；") || "没有沿用的习惯" : value?.text || "（空）";
      for (const change of detail.changes || []) {
        const box = card(change.kind === "fact_conflict" ? "资料冲突与更新" : "相处方式为何改变", change.reason, time(change.at));
        box.append(el("p", "learning-quote", `原先：${describe(change.before)}`), el("p", "learning-quote", `现在：${describe(change.after)}`));
        const sources = el("details"); sources.append(el("summary", "", "查看纠正依据"));
        for (const row of change.evidence || []) sources.append(el("p", "learning-quote", typeof row === "string" ? row : `${row.reason || "本批直接互动"} · 记录 ${(row.source_ids || []).join("、")}`));
        box.append(sources); body.append(box);
      }
      for (const item of detail.history || []) {
        const box = card(item.delta ? `互动评价 ${item.delta > 0 ? "+" : ""}${item.delta}` : "中性互动", item.reason, time(item.at));
        for (const source of item.evidence || []) box.append(el("p", "learning-quote", `${source.name || source.user_id}：${source.text}`));
        body.append(box);
      }
    });
    // A person's record is bound to its conversation, not transferable by changing a dropdown.
    current.querySelector("select").disabled = true;
    return current;
  }
  function openDecisions(session, trace = "") {
    const status = {silent: "主动静默", merged: "并入其他回复", fixed_reply: "固定回应", error: "失败", handled: "已处理",
      awaiting_native: "已交给宿主", generated: "已生成", sent: "已发送"};
    return open("决策与执行记录", session, async ({body, session, trace, active}) => {
      const data = await apiGet("decision-traces", {session, trace}); if (!active()) return;
      body.replaceChildren(el("p", "muted", "记录插件实际观察到的决定和结果。只有交给宿主而没有后续记录，表示结果尚未观察到，不能当作主动沉默或确认失败。"));
      for (const item of data.items || []) {
        const box = card(status[item.status] || item.status, item.reason, time(item.at));
        const details = el("details"); details.append(el("summary", "", "查看关联依据"));
        details.append(el("pre", "learning-code", JSON.stringify({trace_id: item.trace_id, stage: item.stage, ...item.details}, null, 2)));
        box.append(details); body.append(box);
      }
      if (!data.items?.length) body.append(card("还没有相关记录", "收到消息并进入本插件回复流程后，这里会留下记录。"));
    }, trace);
  }
  return {openVocabulary, openPerson, openDecisions};
}
