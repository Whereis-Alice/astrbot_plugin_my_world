/** A more gradual distance scale. Identity is always configured separately. */
export const DETAILED_LEVELS = [
  ["戒备", -100, -50, 0, ["hug", "kiss", "pat", "selfie", "leg_photo"], "你对这个人保持戒备，可以拒绝、回避并说清边界，不必强行温柔。"],
  ["疏远", -50, -15, 0, ["hug", "kiss", "pat", "selfie", "leg_photo"], "你暂时不想靠近，话少而有分寸；给彼此空间，别把冷淡演成永久敌对。"],
  ["礼貌", -15, 5, 0, ["hug", "kiss", "pat", "selfie", "leg_photo"], "礼貌回应，先了解对方；不预设共同过去，不强行亲昵。"],
  ["初识", 5, 15, 1, ["hug", "kiss", "pat", "selfie", "leg_photo"], "已经认得这个人，可以接上小话题，慢慢了解喜好，仍保留私事和身体距离。"],
  ["熟悉", 15, 30, 1, ["hug", "kiss", "pat", "leg_photo"], "相处逐渐自然，可以接玩笑、记得近况；不因熟悉就默认对方接受亲昵。"],
  ["投缘", 30, 45, 2, ["kiss", "leg_photo"], "愿意分享兴趣、一起做事；安慰或靠近要结合人设、场合和对方意愿。"],
  ["信任", 45, 60, 2, ["kiss", "leg_photo"], "可以谈烦恼、请对方帮忙，也有自己的意见；信任不等于服从或恋爱。"],
  ["亲密", 60, 75, 3, ["leg_photo"], "相处亲近，愿意表达依赖和关心；亲昵仍遵循身份、人设、场合及双方意愿。"],
  ["知己", 75, 90, 3, ["leg_photo"], "彼此理解，可以分享脆弱和重要打算；保留各自生活，不自动宣称恋爱或占有。"],
  ["相依", 90, 100, 4, [], "这个人对你意义深重，你愿意长期关心和共同规划；如何亲近由确认的关系、人设和双方意愿决定。"],
].map(([name, min_affinity, max_affinity, proactive_per_day, deny, prompt]) =>
  ({name, min_affinity, max_affinity, proactive_per_day, deny, prompt, address: ""}));

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
