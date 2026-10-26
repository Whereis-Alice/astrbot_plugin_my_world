/** Shared relationship grouping and attached progression conditions. */
const resolveBond = (profile, name) => (profile.bonds || []).find(b => b.name === name)
  || (profile.bonds || []).find(b => (b.aliases || []).includes(name));

export function relationshipGroups(profile, preset) {
  const ordinary = new Set((preset?.bonds || []).map(b => b.name));
  const ordered = (preset?.bonds || []).map(b => b.name);
  if (!ordered.includes(profile.default_bond)) ordered.unshift(profile.default_bond);
  for (const stage of profile.friendship_stages || []) {
    const bond = resolveBond(profile, stage.bond);
    if (bond && bond.group && !bond.unique && !bond.negative) {ordinary.add(bond.name); ordered.push(bond.name);}
  }
  const rank = name => {const i = ordered.indexOf(name); return i < 0 ? ordered.length : i;};
  const groups = [
    {kind: "ordinary", label: "普通关系", items: []},
    {kind: "special", label: "特殊关系", items: []},
  ];
  (profile.bonds || []).forEach((bond, index) => {
    groups[(ordinary.has(bond.name) || bond.group) && !bond.unique && !bond.negative ? 0 : 1].items.push({bond, index});
  });
  groups[0].items.sort((a, b) => rank(a.bond.name) - rank(b.bond.name) || a.index - b.index);
  groups[1].items.sort((a, b) => Number(Boolean(a.bond.negative)) - Number(Boolean(b.bond.negative)) || a.index - b.index);
  return groups;
}

export function renameRelationship(profile, bond, name) {
  const stages = (profile.friendship_stages || []).filter(s => resolveBond(profile, s.bond) === bond);
  if (profile.default_bond === bond.name) profile.default_bond = name;
  bond.name = name;
  stages.forEach(stage => {stage.bond = name;});
}

export function setInitialRelationship(profile, name) {
  profile.default_bond = name;
  // A deliberate selection supersedes legacy per-row initial markers.
  (profile.bonds || []).forEach(bond => {delete bond.initial;});
}

export function setRelationshipAliases(profile, bond, aliases) {
  (profile.friendship_stages || []).filter(s => resolveBond(profile, s.bond) === bond)
    .forEach(stage => {stage.bond = bond.name;});
  bond.aliases = aliases;
}

export function removeRelationship(profile, bond) {
  profile.friendship_stages = (profile.friendship_stages || []).filter(s => resolveBond(profile, s.bond) !== bond);
  profile.bonds = (profile.bonds || []).filter(item => item !== bond);
  if (profile.default_bond === bond.name) setInitialRelationship(profile, profile.bonds[0]?.name || "");
}

/** One condition editor belongs to its ordinary relationship, never another table. */
export function relationshipStageEditor({profile, bond, preset, el, onChange = () => {}, onError = () => {}}) {
  const find = () => (profile.friendship_stages || []).find(s => resolveBond(profile, s.bond) === bond);
  if (bond.name === profile.default_bond && !find()) return null;
  let savedStage = find();
  const box = el("details", "relationship-growth");
  const head = el("summary");
  const status = el("span");
  head.append(el("strong", "", "自然发展条件"), status);
  const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = Boolean(find());
  const toggle = el("label", "rule-check"); toggle.append(enabled, el("span", "", "按相处推进到此关系"));
  const fields = el("div", "relationship-growth-fields");
  const inputs = [];
  for (const [key, label, min, max] of [["min_talk_days", "不同日期直接交谈", 1, 366], ["min_affinity", "至少好感", -100, 100]]) {
    const wrap = el("label", "rule-field"); const input = el("input");
    input.type = "number"; input.min = min; input.max = max; input.step = key === "min_talk_days" ? 1 : "any";
    input.addEventListener("input", () => {
      const stage = find(); const value = Number(input.value);
      if (!stage || input.value === "" || !Number.isFinite(value)) return;
      stage[key] = Math.max(min, Math.min(max, key === "min_talk_days" ? Math.round(value) : value));
      refresh(); onChange();
    });
    input.addEventListener("change", () => {if (find()) input.value = find()[key];});
    wrap.append(el("span", "", label), input); fields.append(wrap); inputs.push({key, input});
  }
  function refresh() {
    const stage = find(); enabled.checked = Boolean(stage); fields.hidden = !stage;
    inputs.forEach(({key, input}) => {if (document.activeElement !== input) input.value = stage?.[key] ?? "";});
    status.textContent = stage ? `${stage.min_talk_days} 个日期 · 好感 ${stage.min_affinity}` : "未配置阶段";
  }
  enabled.addEventListener("change", () => {
    if (enabled.checked && !find()) {
      profile.friendship_stages = profile.friendship_stages || [];
      if (profile.friendship_stages.length >= 12) {refresh(); onError("最多配置 12 个普通关系阶段"); return;}
      const template = preset?.stages?.find(s => s.bond === bond.name);
      const stage = {...(savedStage || template || {min_talk_days: 7, min_affinity: 20}), bond: bond.name};
      // Keep the displayed ordinary order when restoring a missing stage.
      const names = relationshipGroups(profile, preset)[0].items.map(item => item.bond.name);
      const index = profile.friendship_stages.findIndex(s => names.indexOf(resolveBond(profile, s.bond)?.name) > names.indexOf(bond.name));
      profile.friendship_stages.splice(index < 0 ? profile.friendship_stages.length : index, 0, stage);
    } else if (!enabled.checked) {
      savedStage = find();
      profile.friendship_stages = (profile.friendship_stages || []).filter(s => resolveBond(profile, s.bond) !== bond);
    }
    refresh(); onChange();
  });
  box.append(head, el("p", "muted", "交谈日期与好感需同时达到，且此关系需允许自然形成。只计直接交谈；特殊身份与管理员设定仍受保护。"), toggle, fields);
  refresh(); return box;
}
