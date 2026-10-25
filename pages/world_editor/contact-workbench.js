/** Card directory and an on-demand reading drawer. Requests retain their origin. */
export function createContactWorkbench({ state, root, apiGet, loadMeta,
  renderList, renderPlaces, renderDetail, updateSessionNames, el }) {
  let listRevision = 0, detailRevision = 0, loadedSession = null, opener = null;
  const $ = id => document.getElementById(id);
  const session = () => $("contacts-session")?.value || "";
  const body = () => $("contacts-detail");
  const drawer = $("contact-drawer"), scrim = $("contacts-scrim");

  function message(text, retry) {
    const box = el("div", "people-empty");
    box.setAttribute("role", "status");
    box.appendChild(el("p", "", text));
    if (retry) {
      const button = el("button", "ghost small", "重试");
      button.type = "button";
      button.addEventListener("click", retry);
      box.appendChild(button);
    }
    return box;
  }
  function setPanel() {
    state.contactDetailTab = "profile";
    const box = body();
    if (box && !box.querySelector("[data-contact-page]")) {
      const panel = el("section", "contact-page");
      panel.dataset.contactPage = "profile";
      panel.id = "contact-page-profile";
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", "contact-tab-profile");
      [...box.children].forEach(node => panel.appendChild(node));
      box.appendChild(panel);
    }
    if (box) box.scrollTop = 0;
  }
  function close() {
    ++detailRevision;
    drawer.hidden = true; scrim.hidden = true;
    root.classList.remove("contact-open");
    const currentCard = root.querySelector(`[data-user="${CSS.escape(String(state.contactUser))}"]`);
    (currentCard || opener)?.focus();
  }
  function clearDetail(text) {
    state.contactDetail = null;
    $("contacts-title").textContent = "联系人档案";
    $("contacts-subtitle").textContent = "选择卡片，查看她对这个人的认识。";
    $("contacts-avatar").textContent = "?";
    body().removeAttribute("aria-busy");
    body().replaceChildren(message(text));
    close();
  }
  async function select(userId, { reveal = true } = {}) {
    const request = ++detailRevision, scope = session();
    if (!scope) return;
    state.contactUser = userId; state.contactDetail = null;
    renderList();
    if (reveal) {
      opener = root.querySelector(`[data-user="${CSS.escape(String(userId))}"]`) || document.activeElement;
      drawer.hidden = false; scrim.hidden = false;
      root.classList.add("contact-open");
      $("contacts-back").focus();
    }
    const person = state.contacts.find(item => item.user_id === userId);
    $("contacts-title").textContent = person?.name || userId;
    $("contacts-subtitle").textContent = `QQ ${userId} · 正在读取资料`;
    $("contacts-avatar").textContent = [...String(person?.name || userId)][0];
    body().setAttribute("aria-busy", "true");
    body().replaceChildren(message("正在读取资料与经历…"));
    try {
      const data = await apiGet("profile/contact", { session: scope, user_id: userId });
      if (request !== detailRevision || scope !== session() || state.contactUser !== userId) return;
      state.contactDetail = data;
      renderDetail(data);
    } catch (error) {
      if (request === detailRevision && scope === session())
        body().replaceChildren(message(`读取失败：${error.message || error}`, () => select(userId)));
    } finally {
      if (request === detailRevision) body().removeAttribute("aria-busy");
    }
  }
  async function reload() {
    const request = ++listRevision;
    ++detailRevision;
    const scope = session(), sameScope = loadedSession === scope;
    if (!sameScope) {
      state.contactUser = ""; state.contacts = [];
      clearDetail("选择一位联系人，查看她对这个人的了解。");
    }
    $("contacts-list").setAttribute("aria-busy", "true");
    if (!scope) {
      $("contacts-list").replaceChildren(message("先在会话页添加一个群或私聊。"));
      $("contacts-list").removeAttribute("aria-busy"); return;
    }
    try {
      const [, data] = await Promise.all([loadMeta(), apiGet("profile/contacts", { session: scope })]);
      if (request !== listRevision || scope !== session()) return;
      updateSessionNames?.(data.session_names || {});
      loadedSession = scope;
      state.contacts = data.people || [];
      $("contacts-note").textContent = `关系与好感按会话独立保存 · ${data.weaver_session_based === false ? "结构化资料全局共享" : "结构化资料按会话隔离"} · 睡眠整理${data.consolidate_enabled ? "已开启" : "未开启"}`;
      renderList(); renderPlaces();
      if (!drawer.hidden && state.contacts.some(item => item.user_id === state.contactUser))
        await select(state.contactUser, { reveal: false });
      else if (!drawer.hidden) clearDetail("此联系人已不在当前通讯录中。");
    } catch (error) {
      if (request === listRevision && scope === session())
        $("contacts-list").replaceChildren(message(`通讯录读取失败：${error.message || error}`, reload));
    } finally {
      if (request === listRevision) $("contacts-list").removeAttribute("aria-busy");
    }
  }
  $("contacts-back").addEventListener("click", close);
  scrim.addEventListener("click", close);
  drawer.addEventListener("keydown", event => {
    if (!$("dialog").classList.contains("hidden")) return;
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Tab") {
      const nodes = [...drawer.querySelectorAll('button:not(:disabled), input, select, textarea, summary, [tabindex="0"]')].filter(node => node.getClientRects().length);
      if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes.at(-1)?.focus(); }
      if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0]?.focus(); }
    }
  });
  return { reload, select, setPanel, close };
}
