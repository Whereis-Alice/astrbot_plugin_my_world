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
