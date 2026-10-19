/** Small, text-only primitives shared by the two people pages. */
export const PEOPLE_COLORS = ["#93b8ac", "#b8a4d8", "#d2ad89", "#8fb4cf", "#cb9eac", "#b4bb83"];

export function personColor(id) {
  let hash = 0;
  for (const char of String(id)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return PEOPLE_COLORS[hash % PEOPLE_COLORS.length];
}

export function personAvatar(el, id, name, className = "people-avatar") {
  const avatar = el("span", className, [...String(name || id || "?").trim()][0] || "?");
  avatar.style.setProperty("--person-color", personColor(id));
  avatar.setAttribute("aria-hidden", "true");
  return avatar;
}

export function socialRange(sessions) {
  const values = sessions.map(item => Number(item.affinity)).filter(Number.isFinite);
  if (!values.length) return "—";
  const low = Math.min(...values), high = Math.max(...values);
  const number = value => Number(value.toFixed(1)).toString();
  return low === high ? number(low) : `${number(low)}–${number(high)}`;
}

export function distanceFor(rules, affinity, names) {
  return distancePreview(rules, affinity, names).name;
}

/** Mirror ProfileConfig's distance calculation and make its limits visible. */
export function distancePreview(rules, affinity, names) {
  const levels = rules.levels || [];
  if (!levels.length) return { name: "未设置档位", reason: "请先在关系规则中设置相处档位。" };
  let bonds = (rules.bonds || []).filter(bond => names.includes(bond.name));
  const actual = bonds.length > 0;
  if (!actual) bonds = (rules.bonds || []).filter(bond => bond.name === rules.default_bond);
  let index = levels.findIndex(level => Number(level.min_affinity) <= affinity && affinity < Number(level.max_affinity));
  if (index < 0) {
    index = 0;
    levels.forEach((level, i) => { if (affinity >= Number(level.min_affinity)) index = i; });
  }
  const natural = index;
  const negative = actual ? bonds.filter(bond => bond.negative) : [];
  const floor = negative.length ? 0 : Math.max(0, ...bonds.map(bond => Number(bond.floor || 0)));
  const cap = negative.length ? Math.min(...negative.map(bond => Number(bond.cap || 0)))
    : bonds.length ? Math.max(...bonds.map(bond => actual && bond.allow_boundary_crossing ? levels.length - 1 : Number(bond.cap || 0))) : 2;
  if (affinity >= 0) index = Math.max(index, floor);
  index = Math.max(0, Math.min(levels.length - 1, index, cap));
  const name = levels[index].name;
  const identity = `${actual ? "关系" : "默认关系"}「${(negative.length ? negative : bonds).map(bond => bond.name).join("、") || rules.default_bond || "未指定"}」`;
  let reason = `好感对应「${levels[natural].name}」，当前关系允许采用此档。`;
  if (index < natural) reason = `好感对应「${levels[natural].name}」，受${identity}上限限制。${negative.length ? "负面关系的限制优先，可调整该身份或其最高档。" : "可调整身份或其最高档；已确认身份可按规则允许越界。"}`;
  else if (index > natural) reason = `好感对应「${levels[natural].name}」，由${identity}最低档抬至「${name}」。`;
  else if (affinity < 0) reason += " 负好感时，关系最低档不生效。";
  return { name, reason };
}

/** A compact trigger with native radio keyboard support and local preferences. */
export function bindPeopleSort(menu, options, onChange, hint = "") {
  if (!menu) return options[0][0];
  let saved;
  try { saved = localStorage.getItem(`mw-${menu.id}`); } catch { /* Storage may be disabled. */ }
  let selected = options.some(([value]) => value === saved) ? saved : options[0][0];
  const trigger = document.createElement("summary");
  trigger.setAttribute("aria-label", "排序方式");
  trigger.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4v16m-4-4 4 4 4-4M14 5h7M14 10h5M14 15h3"/></svg>';
  const panel = document.createElement("div");
  panel.className = "command-menu-body people-sort-options";
  const heading = document.createElement("strong"); heading.textContent = "排序方式"; panel.append(heading);
  const updateTitle = () => {
    trigger.title = `排序：${options.find(([value]) => value === selected)[1]}`;
    trigger.setAttribute("aria-label", trigger.title);
  };
  for (const [value, text] of options) {
    const label = document.createElement("label"), radio = document.createElement("input"), name = document.createElement("span");
    radio.type = "radio"; radio.name = menu.id; radio.value = value; radio.checked = selected === value;
    name.textContent = text;
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      selected = value;
      try { localStorage.setItem(`mw-${menu.id}`, value); } catch { /* Keep working without persistence. */ }
      updateTitle(); menu.open = false; trigger.focus(); onChange(value);
    });
    label.append(radio, name); panel.append(label);
  }
  if (hint) { const note = document.createElement("p"); note.textContent = hint; panel.append(note); }
  menu.replaceChildren(trigger, panel); updateTitle();
  const place = () => {
    if (menu.open) menu.style.setProperty("--people-sort-top", `${trigger.getBoundingClientRect().bottom + 7}px`);
  };
  menu.addEventListener("toggle", place);
  window.addEventListener("resize", place);
  return selected;
}
