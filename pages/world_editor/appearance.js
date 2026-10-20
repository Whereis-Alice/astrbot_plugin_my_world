/** Local appearance preferences. No account settings or plugin configuration writes. */
import { sceneMarkup } from "./world-scene.js?v=4.28.0";

export const APPEARANCES = [
  { id: "nocturne", mode: "dark", name: "星屿", note: "星轨 · 冷光玻璃", colors: ["#172033", "#626391", "#a7c9cf"] },
  { id: "aurora", mode: "dark", name: "潮汐", note: "极光 · 深海流线", colors: ["#0d282e", "#2b7c71", "#a2dfcc"] },
  { id: "rose", mode: "dark", name: "晚樱", note: "花影 · 暖夜缎光", colors: ["#31212c", "#86546e", "#e6b6bc"] },
  { id: "cloud", mode: "light", name: "晴空", note: "云层 · 轻盈透光", colors: ["#edf4fa", "#b2cbea", "#fffdf1"] },
  { id: "sand", mode: "light", name: "日晷", note: "刻度 · 温润纸感", colors: ["#f6f0df", "#d8c4a0", "#b5a076"] },
  { id: "jade", mode: "light", name: "竹间", note: "竹影 · 青瓷釉面", colors: ["#eef4e6", "#aec7ae", "#7fa38b"] },
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
  note.textContent = "六座小世界，六种光影与材质。外观仅保存在这台设备。";
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
    sample.innerHTML = sceneMarkup(`preview-${item.id}`);
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
