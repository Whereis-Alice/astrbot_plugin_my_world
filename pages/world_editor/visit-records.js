/** Project recorded facts without turning system metadata into character speech. */
const TEXT_TYPES = {
  say: "speech", invitation: "speech", contact_reply: "speech",
  contact_status: "status", action: "intent", action_plan: "intent",
  experience: "experience", result: "result",
};
const CONTINUATIONS = new Set(["action", "action_plan", "experience"]);

export function recordIdentity(visit, item) {
  if (!item || typeof item.actor_id !== "string" || !item.actor_id) return null;
  const actors = [visit?.local, visit?.remote].filter(Boolean);
  const exact = actors.filter(actor => actor.id && actor.id === item.actor_id);
  const short = actors.filter(actor => actor.actor_id && actor.actor_id === item.actor_id);
  const matches = exact.length ? exact : short;
  return matches.length === 1 ? matches[0] : null;
}

/** type describes the evidence; kind retains its original wire/legacy category. */
export function recordParts(item) {
  if (!item || typeof item !== "object") return [];
  const kind = typeof item.kind === "string" ? item.kind : "";
  const mainType = Object.hasOwn(TEXT_TYPES, kind) ? TEXT_TYPES[kind] : "chronicle";
  const legacy = !kind;
  const neutral = !legacy && ["chronicle", "status"].includes(mainType);
  const parts = [];
  function add(type, text) {
    if (typeof text !== "string" || !text.trim()
        || parts.some(part => part.type === type && part.text === text)) return;
    parts.push({type, text, kind});
  }
  // Legacy composite rows name their speech explicitly. A typed non-speech
  // row retains its own evidence category, even for a field named "say".
  const spoken = mainType === "speech" || legacy;
  if (spoken) add("speech", item.say);
  add(mainType, item.text);
  if (!spoken) add(mainType, item.say);
  if (neutral) {
    add(mainType, item.action);
    add(mainType, item.result);
  } else {
    add("intent", item.action);
    add("result", item.result);
  }
  return parts;
}

function recordedAt(item) {
  const value = Number(item?.at || 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function sameMomentBoundary(item, last) {
  if (!last || item.node_id && last.node_id && item.node_id !== last.node_id) return false;
  const sequenced = Number.isInteger(item.seq) && item.seq > 0
    && Number.isInteger(last.seq) && last.seq > 0;
  const at = recordedAt(item), previousAt = recordedAt(last);
  if (sequenced && item.seq !== last.seq + 1) return false;
  if (at && previousAt && Math.abs(at - previousAt) >= 3) return false;
  return sequenced || Boolean(at && previousAt);
}

/** Preserve row order and references; the caller owns paginated seq merging. */
export function recordMoments(visit, logs) {
  const moments = [];
  for (const item of Array.isArray(logs) ? logs : []) {
    const parts = recordParts(item);
    if (!parts.length) continue;
    const identity = recordIdentity(visit, item);
    const previous = moments.at(-1), last = previous?.items.at(-1);
    const category = parts.some(part => part.type === "speech") ? "speech" : parts[0].type;
    if (CONTINUATIONS.has(item.kind) && identity && previous?.identity === identity
        && previous.category === "speech" && last?.actor_id === item.actor_id
        && !previous.items.some(row => row.kind === item.kind) && sameMomentBoundary(item, last)) {
      previous.items.push(item);
      previous.parts.push(...parts);
    } else {
      moments.push({identity, category, items: [item], parts, at: recordedAt(item)});
    }
  }
  return moments;
}
