import { mountWorldScene } from "./world-scene.js?v=4.29.0";

export function updateWorkspaceContext() {
  const trigger = document.querySelector(".topbar-context > summary");
  const session = document.querySelector(".field-session");
  if (!trigger || !session) return;
  const hasSession = getComputedStyle(session).display !== "none";
  trigger.textContent = hasSession ? "会话与范围" : "世界设置";
  trigger.setAttribute("aria-label", hasSession ? "会话与世界设置" : "世界设置");
}

/** Compact reading chrome, with secondary operations available on demand. */
export function initialiseWorkspace() {
  mountWorldScene();
  const icons = {
    status: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M3 10h18M10 10v11"/>',
    life: '<path d="M12 21V10M12 16C5 16 3 11 3 5c6 0 9 3 9 8M12 12c0-6 3-9 9-9 0 6-3 9-9 9"/>',
    thoughts: '<path d="M5 17a8 8 0 1 1 4 3l-5 1z"/><path d="M8 10h8M8 14h5"/>',
    events: '<path d="m12 3 2.8 5.7L21 10l-4.5 4.4 1.1 6.3L12 17.8l-5.6 2.9 1.1-6.3L3 10l6.2-1.3z"/>',
    contacts: '<rect x="5" y="3" width="15" height="18" rx="3"/><path d="M3 7h4M3 12h4M3 17h4M10 16c0-3 6-3 6 0"/><circle cx="13" cy="9" r="2"/>',
    relationships: '<path d="M12 20s-9-5.5-9-11a5 5 0 0 1 9-3 5 5 0 0 1 9 3c0 5.5-9 11-9 11z"/>',
    memories: '<path d="M4 5c3-1 6-1 8 1 2-2 5-2 8-1v15c-3-1-6-1-8 1-2-2-5-2-8-1zM12 6v15"/>',
    logs: '<path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7"/>',
    "model-calls": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 15v-3m5 3V8m5 7v-5"/>',
    ticks: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    debug: '<path d="m8 7-4 5 4 5m8-10 4 5-4 5m-3-14-2 18"/>',
    map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15"/>',
    actions: '<path d="m9 3 12 9-12 9z"/>',
    schedules: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4M17 3v4M3 11h18M8 15h2M14 15h2"/>',
    sessions: '<path d="M21 11a8 8 0 0 1-8 8H8l-5 3V7a4 4 0 0 1 4-4h6a8 8 0 0 1 8 8zM7 9h10M7 13h6"/>',
    settings: '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
    presets: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
  };
  document.querySelectorAll("#tabs button[data-tab]").forEach(button => {
    if (button.classList.contains("active")) button.setAttribute("aria-current", "page");
    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = button.textContent.trim();
    button.replaceChildren(label);
    button.setAttribute("aria-label", label.textContent);
    button.title = label.textContent;
    const icon = document.createElement("span");
    icon.className = "nav-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = `<svg viewBox="0 0 24 24">${icons[button.dataset.tab] || icons.status}</svg>`;
    button.prepend(icon);
  });
  const context = document.createElement("details");
  context.className = "command-menu topbar-context";
  const trigger = document.createElement("summary");
  trigger.textContent = "会话与范围";
  trigger.setAttribute("aria-label", "会话与世界设置");
  const settings = document.createElement("div");
  settings.className = "command-menu-body";
  context.append(trigger, settings);
  document.querySelector(".topbar-right").prepend(context);
  const relocations = [...document.querySelectorAll(".topbar-left > :is(.field-session, .field-scope, .proactive-control)")].map(control => {
    const anchor = document.createComment("workspace context position");
    control.before(anchor);
    return { control, anchor };
  });
  const compact = matchMedia("(max-width: 760px)");
  const placeContext = () => {
    for (const { control, anchor } of relocations) {
      if (compact.matches) settings.append(control);
      else anchor.after(control);
    }
    if (!compact.matches) context.open = false;
  };
  compact.addEventListener("change", placeContext);
  placeContext();
  updateWorkspaceContext();
  for (const root of document.querySelectorAll("main > .tab")) {
    const head = root.querySelector(":scope > .page-head");
    if (!head) continue;
    for (const hint of root.querySelectorAll(":scope > .hint-line")) {
      const help = document.createElement("details");
      help.className = "command-menu workspace-help";
      const summary = document.createElement("summary");
      summary.textContent = "说明";
      const content = document.createElement("div");
      content.className = "command-menu-body";
      content.append(hint);
      help.append(summary, content);
      (head.querySelector(".page-head-right") || head).append(help);
    }
    const secondary = head.querySelector(".page-head-right > .toolbar + .toolbar");
    if (secondary) {
      const menu = document.createElement("details");
      menu.className = "command-menu workspace-batch";
      const summary = document.createElement("summary");
      summary.textContent = "批量管理";
      const content = document.createElement("div");
      content.className = "command-menu-body";
      content.append(...secondary.children);
      menu.append(summary, content);
      secondary.replaceWith(menu);
    }
  }
  // Menus close together and on Escape/outside click; selection stays on the page.
  const selector = "details.command-menu, details.contacts-management, details.directory-options, details.contacts-view-menu";
  document.addEventListener("click", event => {
    document.querySelectorAll(`${selector.split(", ").map(item => `${item}[open]`).join(", ")}`).forEach(menu => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  // Let the native picker consume Escape before surrounding menus or dialogs.
  // Propagation stops here; its default close action still runs.
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const select = event.target.closest?.("select");
    if (select && CSS.supports("selector(select:open)") && select.matches(":open")) event.stopPropagation();
  }, true);
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    for (const menu of document.querySelectorAll(`${selector.split(", ").map(item => `${item}[open]`).join(", ")}`)) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    }
  });
}
