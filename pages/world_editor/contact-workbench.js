/** Address-book lifecycle. Each pane scrolls independently; stale requests never
 * repaint another contact or conversation. DOM helpers and API are injected so
 * this component also runs inside AstrBot's sandboxed Plugin Page. */
export function createContactWorkbench({ state, root, apiGet, loadMeta,
  renderList, renderPlaces, renderDetail, el }) {
  let listRevision = 0;
  let detailRevision = 0;
  let loadedSession = null;
  const $ = (id) => document.getElementById(id);
  const session = () => $("contacts-session")?.value || "";
  const body = () => $("contacts-detail");

  function message(text, retry) {
    const box = el("div", "contacts-empty");
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

  function setPanel(name) {
    state.contactDetailTab = name === "relation" ? "relation" : "profile";
    const box = body();
    // Render helpers produce sections. Mount them into real tab panels once.
    if (box && !box.querySelector("[data-contact-page]")) {
      const children = [...box.children];
      for (const key of ["profile", "relation"]) {
        const panel = el("section", "contact-page");
        panel.dataset.contactPage = key;
        panel.id = `contact-page-${key}`;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", `contact-tab-${key}`);
        children.filter((node) => node.dataset.contactPanel !== "relation"
          ? key === "profile" : key === "relation").forEach((node) => panel.appendChild(node));
        box.appendChild(panel);
      }
    }
    root.querySelectorAll("[data-contact-detail-tab]").forEach((button) => {
      const selected = button.dataset.contactDetailTab === state.contactDetailTab;
      button.classList.toggle("on", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    box?.querySelectorAll("[data-contact-page]").forEach((panel) => {
      panel.hidden = panel.dataset.contactPage !== state.contactDetailTab;
    });
    if (box) box.scrollTop = 0;
  }

  function clearDetail(text) {
    state.contactDetail = null;
    $("contacts-title").textContent = "还没有认识的人";
    $("contacts-subtitle").textContent = "聊天后会自动积累资料与经历。";
    $("contacts-avatar").textContent = "?";
    for (const id of ["contacts-detail-affinity", "contacts-detail-completeness"])
      $(id).classList.add("hidden");
    body().removeAttribute("aria-busy");
    body().replaceChildren(message(text));
    root.classList.remove("contact-open");
  }

  async function select(userId, { reveal = true } = {}) {
    const revision = ++detailRevision;
    const scope = session();
    state.contactUser = userId;
    state.contactDetail = null;
    renderList();
    if (reveal) root.classList.add("contact-open");
    const person = state.contacts.find((item) => item.user_id === userId);
    $("contacts-title").textContent = person?.name || userId;
    $("contacts-subtitle").textContent = `用户 ID ${userId} · 正在读取资料`;
    $("contacts-avatar").textContent = String(person?.name || userId).slice(0, 1);
    for (const id of ["contacts-detail-affinity", "contacts-detail-completeness"])
      $(id).classList.add("hidden");
    body().setAttribute("aria-busy", "true");
    body().replaceChildren(message("正在读取资料与经历…"));
    try {
      const data = await apiGet("profile/contact", { session: scope, user_id: userId });
      if (revision !== detailRevision || scope !== session() || state.contactUser !== userId) return;
      state.contactDetail = data;
      renderDetail(data);
    } catch (error) {
      if (revision !== detailRevision || scope !== session()) return;
      body().replaceChildren(message(`读取失败：${error.message || error}`, () => select(userId)));
    } finally {
      if (revision === detailRevision) body().removeAttribute("aria-busy");
    }
  }

  async function reload() {
    const revision = ++listRevision;
    ++detailRevision;
    const scope = session();
    const sameScope = loadedSession === scope;
    if (!sameScope) {
      state.contactUser = "";
      state.contacts = [];
      clearDetail("选择一位联系人，查看她对这个人的了解。");
    }
    $("contacts-list").setAttribute("aria-busy", "true");
    try {
      const [, data] = await Promise.all([loadMeta(), apiGet("profile/contacts", { session: scope })]);
      if (revision !== listRevision || scope !== session()) return;
      loadedSession = scope;
      state.contacts = data.people || [];
      $("contacts-note").textContent =
        `${data.weaver_session_based === false ? "画像全局共享" : "画像按当前会话隔离"} · ` +
        `睡眠整理${data.consolidate_enabled ? "已开启" : "未开启"} · 当前说话人的资料自动带给模型`;
      if (!state.contacts.some((item) => item.user_id === state.contactUser))
        state.contactUser = state.contacts[0]?.user_id || "";
      renderList();
      renderPlaces();
      if (state.contactUser) await select(state.contactUser, { reveal: sameScope && root.classList.contains("contact-open") });
      else clearDetail("当前会话还没有联系人。她认识新朋友后，会在这里留下记录。");
    } catch (error) {
      if (revision !== listRevision || scope !== session()) return;
      $("contacts-list").replaceChildren(message(`通讯录读取失败：${error.message || error}`, reload));
    } finally {
      if (revision === listRevision) $("contacts-list").removeAttribute("aria-busy");
    }
  }

  $("contacts-back").addEventListener("click", () => root.classList.remove("contact-open"));
  $("contacts-detail-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = state.contactDetailTab === "relation";
    const key = event.key === "Home" ? "profile" : event.key === "End" ? "relation" : current ? "profile" : "relation";
    setPanel(key);
    $(`contact-tab-${key}`).focus();
  });
  return { reload, select, setPanel };
}
