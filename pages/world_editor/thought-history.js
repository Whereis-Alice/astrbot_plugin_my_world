/** Date navigation for both thought categories, using the world's saved date. */
export function thoughtDate(row) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(row.date || "")) return row.date;
  const stamp = Number(row.at || 0);
  if (!(stamp > 0)) return "undated";
  const date = new Date(stamp * 1000);
  if (!Number.isFinite(date.getTime())) return "undated";
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
}

export function updateThoughtDates(select, rows, selected = "") {
  if (!select) return;
  const counts = new Map();
  for (const row of rows) {
    const date = thoughtDate(row);
    counts.set(date, (counts.get(date) || 0) + 1);
  }
  const options = [new Option(`全部日期 · ${rows.length} 条`, "")];
  for (const date of [...counts.keys()].filter(date => date !== "undated").sort().reverse())
    options.push(new Option(`${date} · ${counts.get(date)} 条`, date));
  if (counts.has("undated")) options.push(new Option(`未记录日期 · ${counts.get("undated")} 条`, "undated"));
  if (selected && !counts.has(selected)) options.push(new Option(`${selected === "undated" ? "未记录日期" : selected} · 无记录`, selected));
  select.replaceChildren(...options);
  select.value = selected;
}
