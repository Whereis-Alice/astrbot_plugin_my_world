/** Local appearance preferences. No account settings or plugin configuration writes. */
export const APPEARANCES = [
  { id: "nocturne", mode: "dark", name: "星幕", note: "靛蓝 · 鸢尾紫", colors: ["#11162b", "#a99bff", "#70d8df"] },
  { id: "aurora", mode: "dark", name: "极光", note: "深海 · 冰川绿", colors: ["#0c252a", "#66dcc6", "#86b8ff"] },
  { id: "rose", mode: "dark", name: "绯夜", note: "烟紫 · 玫瑰金", colors: ["#291c2a", "#f2a8bc", "#d5bc8b"] },
  { id: "cloud", mode: "light", name: "云白", note: "雾蓝 · 薰衣草", colors: ["#eef0f9", "#6d5fe9", "#258599"] },
  { id: "sand", mode: "light", name: "月砂", note: "暖白 · 琥珀", colors: ["#f4efe7", "#a36337", "#8b728f"] },
  { id: "jade", mode: "light", name: "青瓷", note: "玉白 · 松石绿", colors: ["#eaf3ef", "#237b6e", "#7e74ab"] },
];

let dialog, onChange = () => {};
const current = () => APPEARANCES.find(item => item.id === document.documentElement.dataset.palette) || APPEARANCES[0];

function apply(item, persist = true) {
  document.documentElement.dataset.theme = item.mode;
  document.documentElement.dataset.palette = item.id;
  if (persist) {
    try {
      localStorage.setItem("mw-appearance", item.id);
      localStorage.setItem("mw-theme", item.mode);
    } catch { /* The current view still works when local storage is unavailable. */ }
  }
  const button = document.getElementById("theme-toggle");
  button.textContent = "◈ 外观";
  button.title = `当前外观：${item.name} · 选择配色`;
  button.setAttribute("aria-label", `外观与配色，当前${item.name}`);
  dialog?.querySelectorAll("[data-appearance]").forEach(option => {
    option.setAttribute("aria-pressed", String(option.dataset.appearance === item.id));
  });
  onChange();
}

export function initialiseAppearance({ changed } = {}) {
  onChange = changed || onChange;
  dialog = document.createElement("dialog");
  dialog.id = "appearance-dialog";
  dialog.className = "appearance-dialog";
  dialog.setAttribute("aria-labelledby", "appearance-title");
  const header = document.createElement("header");
  const heading = document.createElement("div");
  const title = document.createElement("h2");
  title.id = "appearance-title";
  title.textContent = "为世界换一种光";
  const note = document.createElement("p");
  note.textContent = "六种配色，选择会保存在这台设备上。";
  heading.append(title, note);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "ghost appearance-close";
  close.textContent = "×";
  close.setAttribute("aria-label", "关闭外观设置");
  close.addEventListener("click", () => dialog.close());
  header.append(heading, close);
  const grid = document.createElement("div");
  grid.className = "appearance-grid";
  for (const item of APPEARANCES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "appearance-option";
    button.dataset.appearance = item.id;
    button.setAttribute("aria-pressed", "false");
    const sample = document.createElement("span");
    sample.className = "appearance-sample";
    item.colors.forEach((color, i) => sample.style.setProperty(`--sample-${i}`, color));
    sample.setAttribute("aria-hidden", "true");
    const name = document.createElement("strong");
    name.textContent = item.name;
    const description = document.createElement("small");
    description.textContent = `${item.mode === "dark" ? "深色" : "浅色"} · ${item.note}`;
    button.append(sample, name, description);
    button.addEventListener("click", () => apply(item));
    grid.append(button);
  }
  dialog.append(header, grid);
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  dialog.addEventListener("close", () => document.getElementById("theme-toggle").focus());
  document.body.append(dialog);
  apply(current(), false);
}

export function openAppearancePicker() {
  if (dialog && !dialog.open) dialog.showModal();
}
