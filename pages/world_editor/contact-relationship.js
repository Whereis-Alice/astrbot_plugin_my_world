/** Relationship explanations use the same read model as the conversation. */
export function renderRelationshipOverview(data, { el, formatStamp }) {
  const person = data.person || {};
  const relation = data.relationship || person.relationship;
  if (!relation) return null;
  const section = el("section", "contact-section contact-relationship-overview");
  section.dataset.contactPanel = "relation";
  section.appendChild(el("h3", "", "现在怎样相处"));
  const grid = el("div", "contact-distance-grid");
  for (const [label, value] of [
    ["已确认的关系", (person.affinities || []).join("、") || "尚未确立"],
    ["当前距离", relation.effective_level || person.level?.name || "尚无记录"],
    ["平常的距离", relation.base_level || "尚无记录"],
    ["直接交谈", `${relation.progress?.talk_days || 0} 个不同日期`],
  ]) {
    const cell = el("div", "contact-distance-cell");
    cell.appendChild(el("span", "muted", label));
    cell.appendChild(el("strong", "", value));
    grid.appendChild(cell);
  }
  section.appendChild(grid);
  const trend = relation.trend;
  if (trend) {
    const detail = el("details", "contact-trend"); detail.append(el("summary", "", `${trend.label} · ${trend.count} 条依据`), el("p", "hint-line", trend.explanation));
    for (const item of trend.evidence || []) detail.append(el("p", "contact-relation-reason", `${formatStamp(item.at)} · ${item.reason}`));
    section.append(detail);
  }
  section.appendChild(el("p", "hint-line", person.level?.prompt || "相处方式会参考人设、关系和最近的经历。"));
  for (const reason of relation.reasons || []) section.appendChild(el("p", "contact-relation-reason", reason));
  if ((person.level?.deny || []).length)
    section.appendChild(el("p", "hint-line", `当前需要保持的边界：${person.level.deny.join("、")}`));
  const progress = relation.progress || {};
  const next = progress.next;
  const progressText = !progress.enabled
    ? "普通友谊的自动发展已关闭，可在设置中开启。"
    : next
      ? `逐渐熟悉：成为「${next.bond}」需要在 ${next.min_talk_days} 个不同日期直接交谈，且好感达到 ${next.min_affinity}。`
        + `目前还差 ${next.days_left} 天、${next.affinity_left} 好感。`
      : "已有关系继续随相处发展；恋爱、身份关系与关系结束需要明确的经历和确认。";
  section.appendChild(el("p", "hint-line contact-friendship-progress", progressText));
  if (person.last_talked_at)
    section.appendChild(el("p", "hint-line", `上次直接交谈：${formatStamp(person.last_talked_at)}`));
  return section;
}

export function renderRelationshipHistory(data, { el, formatStamp }) {
  const section = el("details", "contact-section contact-relationship-history");
  section.dataset.contactPanel = "relation";
  section.appendChild(el("summary", "", "关系与好感的变化依据"));
  const records = (data.bonds || []).map(item => ({
    at: Number(item.until || item.since || 0),
    date: item.status === "past" ? item.until_text : item.since_text,
    label: `${item.type} · ${{ current: "已确认", claimed: "待确认", past: "已结束" }[item.status] || item.status}`,
    reason: item.evidence || "没有保留原文依据",
    source: item.asserted_by || "来源未记录",
  }));
  for (const item of (data.affinity_logs || []).slice(0, 8)) records.push({
    at: Number(item.at || 0), label: `好感 ${Number(item.delta) >= 0 ? "+" : ""}${Number(item.delta).toFixed(2)}`,
    reason: item.reason || "没有记录原因", source: "相处记录",
  });
  records.sort((a, b) => b.at - a.at);
  for (const item of records.slice(0, 16)) {
    const row = el("div", "contact-relation-event");
    row.appendChild(el("strong", "", item.label));
    row.appendChild(el("span", "muted", `${item.date || (item.at ? formatStamp(item.at) : "日期未记录")} · ${item.source}`));
    row.appendChild(el("p", "", item.reason));
    section.appendChild(row);
  }
  if (!records.length) section.appendChild(el("p", "hint-line", "还没有关系变化记录。"));
  return section;
}

export function describeMiss(info, threshold) {
  const score = Number(info.value || 0);
  if (info.waiting) return `刚交谈过，约 ${Number(info.ready_in_minutes || 0)} 分钟后开始积累想念。`;
  const value = `想念 ${score.toFixed(2)} / ${Number(threshold || 0.7).toFixed(2)}。`;
  if (info.reason) return value + info.reason;
  return value + (score >= Number(threshold || 0.7)
    ? "有想联系的念头，是否开口还要看休息、冷却和联系额度。" : "各自生活时，也会慢慢想起这个人。");
}
