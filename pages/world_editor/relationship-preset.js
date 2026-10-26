/** A more gradual distance scale. Identity is always configured separately. */
export const DETAILED_LEVELS = [
  ["戒备", -100, -50, "目前彼此的相处距离很远。"], ["疏远", -50, -15, "目前彼此的相处距离较远。"],
  ["礼貌", -15, 5, "你对这个人还不熟悉，了解有限。"], ["初识", 5, 15, "你刚开始认识这个人，了解还不多。"],
  ["熟悉", 15, 30, "你已经认识这个人，逐渐熟悉。"], ["投缘", 30, 45, "你对这个人已有熟悉感，相处较自然。"],
  ["信任", 45, 60, "你对这个人较熟悉，了解较多。"], ["亲密", 60, 75, "你对这个人很熟悉，相处距离很近。"],
  ["知己", 75, 90, "你对这个人很熟悉，有较深的了解。"], ["相依", 90, 100, "你对这个人非常熟悉，相处距离很近。"],
].map(([name, min_affinity, max_affinity, familiarity_description]) =>
  ({name, min_affinity, max_affinity, familiarity_description, deny: [], deny_customized: false, prompt: "", prompt_customized: false}));

export function applyDetailedLevels(profile) {
  const old = profile.levels || [];
  const indexFor = value => {
    const index = DETAILED_LEVELS.findIndex(level => value >= level.min_affinity && value < level.max_affinity);
    return index >= 0 ? index : value < DETAILED_LEVELS[0].min_affinity ? 0 : DETAILED_LEVELS.length - 1;
  };
  for (const bond of profile.bonds || []) {
    const floor = old[Math.max(0, Number(bond.floor || 0))];
    const cap = old[Math.max(0, Number(bond.cap || 0))];
    bond.floor = floor ? indexFor(Number(floor.min_affinity)) : 0;
    bond.cap = cap ? indexFor(Number(cap.max_affinity) - 0.000001) : DETAILED_LEVELS.length - 1;
    bond.floor = Math.min(bond.floor, bond.cap);
  }
  profile.levels = structuredClone(DETAILED_LEVELS);
}

/** Build a new draft; existing special identities and distance rules survive. */
export function buildFriendshipPreset(profile, preset) {
  if (!preset?.bonds?.length || !preset?.stages?.length) throw new Error("五阶段方案尚未加载，请重新打开页面");
  const next = structuredClone(profile);
  next.bonds = next.bonds || [];
  const names = new Set(preset.bonds.map(bond => bond.name));
  const levelAt = value => {
    let index = 0;
    (next.levels || []).forEach((level, i) => {if (value >= Number(level.min_affinity)) index = i;});
    return index;
  };
  for (const template of preset.bonds) {
    let bond = next.bonds.find(item => item.name === template.name);
    if (bond?.unique || bond?.negative) throw new Error(`「${template.name}」已被设为特殊或负面关系，请先核对关系表`);
    if (!bond) {
      bond = structuredClone(template);
      bond.floor = levelAt(preset.levels[template.floor]?.min_affinity ?? 0);
      bond.cap = levelAt(Number(preset.levels[template.cap]?.max_affinity ?? 100) - 0.000001);
      bond.floor = Math.min(bond.floor, bond.cap);
      next.bonds.push(bond);
    }
    if (template.name !== preset.default_bond) {
      bond.auto_form = true;
      bond.group = template.group;
    }
  }
  // An old "闺蜜" alias must not claim the new ordinary identity "挚友".
  for (const bond of next.bonds) {
    bond.aliases = (bond.aliases || []).filter(alias => !names.has(alias) || alias === bond.name);
    // The reviewed preset replaces the initial identity. Legacy row markers
    // would otherwise override default_bond again during server validation.
    delete bond.initial;
  }
  next.default_bond = preset.default_bond;
  next.friendship_stages = structuredClone(preset.stages);
  return next;
}

/** The same preview is used by settings and the dedicated relationship page. */
export function friendshipPresetControls({profile, preset, el, onApplied, onError}) {
  const box = el("details", "friendship-preset");
  box.appendChild(el("summary", "", "五阶段普通友谊方案"));
  if (!preset) {box.appendChild(el("p", "hint-line", "重新加载页面后可查看方案。")); return box;}
  box.appendChild(el("p", "friendship-chain", [preset.default_bond, ...preset.stages.map(stage => stage.bond)].join(" → ")));
  const list = el("ul");
  preset.stages.forEach(stage => list.appendChild(el("li", "", `${stage.bond}：直接交谈 ${stage.min_talk_days} 个不同日期，好感至少 ${stage.min_affinity}`)));
  box.appendChild(list);
  box.appendChild(el("p", "hint-line", "添加缺失的普通关系并替换发展条件，开启这些关系的自然形成。已有特殊身份、相处档位、说明和自动发展总开关保留；应用后仍需保存。"));
  const apply = el("button", "small ghost", "应用五阶段到当前草稿");
  apply.type = "button";
  apply.addEventListener("click", () => {
    try {Object.assign(profile, buildFriendshipPreset(profile, preset)); onApplied();}
    catch (error) {onError(error.message || "友谊方案未应用");}
  });
  box.appendChild(apply);
  return box;
}
