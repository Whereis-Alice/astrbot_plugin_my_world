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

/** Complete missing ordinary types while keeping existing choices and conditions. */
export function completeOrdinaryRelationships(profile, preset) {
  if (!preset?.bonds?.length || !preset?.stages?.length) throw new Error("普通关系默认值尚未加载，请重新打开页面");
  const next = structuredClone(profile);
  next.bonds = next.bonds || [];
  const added = new Set();
  const levelAt = value => {
    let index = 0;
    (next.levels || []).forEach((level, i) => {if (value >= Number(level.min_affinity)) index = i;});
    return index;
  };
  for (const template of preset.bonds) {
    let bond = next.bonds.find(item => item.name === template.name);
    if (bond?.negative || (bond && preset.special_bonds?.some(item => item.slot && item.slot === bond.slot)))
      throw new Error(`「${template.name}」已被设为特殊或负面关系，请先核对关系表`);
    if (!bond) {
      bond = structuredClone(template);
      bond.floor = levelAt(preset.levels[template.floor]?.min_affinity ?? 0);
      bond.cap = levelAt(Number(preset.levels[template.cap]?.max_affinity ?? 100) - 0.000001);
      bond.floor = Math.min(bond.floor, bond.cap);
      next.bonds.push(bond);
      added.add(bond.name);
    }
  }
  // An old "闺蜜" alias must not claim the new ordinary identity "挚友".
  for (const bond of next.bonds) {
    bond.aliases = (bond.aliases || []).filter(alias => !added.has(alias));
  }
  if (!next.bonds.some(bond => bond.name === next.default_bond)) next.default_bond = preset.default_bond;
  if (!Array.isArray(next.friendship_stages)) next.friendship_stages = structuredClone(preset.stages);
  else if (next.friendship_stages.length) {
    next.friendship_stages.push(...structuredClone(preset.stages.filter(stage => added.has(stage.bond)
      && !next.friendship_stages.some(existing => existing.bond === stage.bond))));
  }
  if (next.friendship_stages.length > 12) throw new Error("普通关系阶段已满，请先停用一个不需要的发展阶段");
  return next;
}

export function ordinaryRelationshipCompletionButton({profile, preset, el, onApplied, onError}) {
  if (!preset?.bonds?.some(template => !(profile.bonds || []).some(bond => bond.name === template.name))) return null;
  const apply = el("button", "small ghost", "补齐普通关系");
  apply.type = "button";
  apply.title = "补齐缺少的普通关系；保留已有关系规则与发展条件，保存后生效。";
  apply.addEventListener("click", () => {
    try {Object.assign(profile, completeOrdinaryRelationships(profile, preset)); onApplied();}
    catch (error) {onError(error.message || "普通关系未补齐");}
  });
  return apply;
}
