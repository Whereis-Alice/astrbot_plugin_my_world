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
  const levels = rules.levels || [];
  if (!levels.length) return "未设置档位";
  let bonds = (rules.bonds || []).filter(bond => names.includes(bond.name));
  const actual = bonds.length > 0;
  if (!actual) bonds = (rules.bonds || []).filter(bond => bond.name === rules.default_bond);
  let index = levels.findIndex(level => Number(level.min_affinity) <= affinity && affinity < Number(level.max_affinity));
  if (index < 0) {
    index = 0;
    levels.forEach((level, i) => { if (affinity >= Number(level.min_affinity)) index = i; });
  }
  const negative = actual ? bonds.filter(bond => bond.negative) : [];
  if (negative.length) index = Math.min(index, ...negative.map(bond => Number(bond.cap || 0)));
  else if (bonds.length) {
    if (affinity >= 0) index = Math.max(index, ...bonds.map(bond => Number(bond.floor || 0)));
    index = Math.min(index, Math.max(...bonds.map(bond => actual && bond.allow_boundary_crossing ? levels.length - 1 : Number(bond.cap || 0))));
  }
  return levels[Math.max(0, Math.min(levels.length - 1, index))]?.name || "未设置档位";
}
