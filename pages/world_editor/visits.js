/** Paired worlds use their own channel; the editor only invites and observes. */
import {recordMoments, recordParts} from "./visit-records.js";

const STATUS = {
  invited: "邀请已提出", inviting: "正在提出邀请", offered: "等待接待决定", pending: "等待答复", accepted: "对方已接受",
  visiting: "正在做客", active: "相见中", arrived: "已经到访", returning: "正在告别归家",
  completed: "已告别归家", closed: "已经结束", ended: "已经结束", declined: "这次没有成行",
  rejected: "对方暂时不方便", cancelled: "邀请已取消", interrupted: "这次暂时中断",
  expired: "这次已结束", failed: "这次没有成行",
};
const WAITING = new Set(["invited", "inviting", "offered", "pending", "accepted"]);

function stamp(value) {
  if (!value) return "";
  const date = new Date(Number(value) * (Number(value) < 1e12 ? 1000 : 1));
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
}

function recordDate(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  const date = new Date(number * (number < 1e12 ? 1000 : 1));
  return Number.isNaN(date.getTime()) ? null : date;
}

function clockStamp(value) {
  return recordDate(value)?.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"}) || "";
}

export function createVisitsPage({root, apiGet, apiPost, el, toast, getSession, confirm}) {
  let revision = 0, globalRevision = 0, renderedGlobalRead = 0, secretRevision = 0, busy = false;
  let data = null, lifeData = null, lifeSession = null, refreshTimer, secretTimer, scopeDraft = null, phoneConnection = null;
  const friendDrafts = new Map();
  const recordDrafts = new Map();
  let historyLimit = 20;
  const heading = el("div", "visits-heading");
  const title = el("div", "visits-title"); title.append(el("span", "visits-title-mark", "↗"), el("h2", "", "友邻与串门"));
  const toolbar = el("div", "toolbar"), refresh = el("button", "ghost", "刷新"), home = el("button", "ghost", "告别归家");
  refresh.type = home.type = "button"; toolbar.append(refresh, home); heading.append(title, toolbar);
  const notice = el("p", "visits-notice muted"); notice.setAttribute("role", "status"); notice.hidden = true;
  const current = el("section", "visits-current card"); current.setAttribute("aria-label", "当前串门");
  const peers = el("section", "visits-peers"); peers.setAttribute("aria-label", "友邻世界列表");
  const phone = el("section", "visits-phone card"); phone.hidden = true; phone.id = "visits-phone";
  phone.setAttribute("aria-label", "打电话");
  const settings = el("details", "visits-settings card");
  const settingsSummary = el("summary", "", "友邻与生活设置"); settings.append(settingsSummary);
  const settingsBody = el("div", "visits-settings-body");
  const lives = el("section", "visits-lives"); lives.setAttribute("aria-label", "参与串门的生活");
  const lifeChoices = el("div", "visits-life-choices");
  const saveScopes = el("button", "primary", "保存参与生活"); saveScopes.type = "button";
  lives.append(el("h3", "", "哪些生活参与串门"),
    el("p", "muted", "友邻只需全局配对一次。勾选参与的生活，共享生活组只需一项；各自的经历仍留在自己的生活里。"), lifeChoices, saveScopes);
  const connections = el("section", "visits-connections"); connections.setAttribute("aria-label", "已配对友邻设置");
  const listenerInfo = el("p", "visits-listener muted");
  const pairing = el("details", "visits-pairing");
  pairing.append(el("summary", "", "添加一个友邻世界"));
  const pairBody = el("div", "visits-pair-body"), generate = el("button", "ghost", "生成一次性配对码"); generate.type = "button";
  pairBody.append(el("h3", "", "把这边的世界介绍给对方"),
    el("p", "muted", "生成全局配对码，把本机串门地址和配对码交给对方管理员。双方只需配对一次，再分别选择参与的生活。"), generate);
  function field(label, placeholder, type = "text") {
    const wrap = el("label", "field"), input = el("input"); input.type = type;
    input.setAttribute("aria-label", label); input.placeholder = placeholder;
    wrap.append(el("span", "", label), input); return {wrap, input};
  }
  const own = field("本次配对码", "生成后会显示在这里"); own.input.readOnly = true;
  own.input.autocomplete = "off"; own.input.spellcheck = false;
  const ownInfo = el("p", "muted"); pairBody.append(own.wrap, ownInfo);
  const form = el("form", "visits-pair-form");
  const address = field("对方串门地址", "http://服务器IP:38471 或 https://你的域名");
  address.input.required = true; address.input.maxLength = 500; address.input.spellcheck = false;
  const foreign = field("对方的一次性配对码", "由对方管理员生成", "password");
  foreign.input.required = true; foreign.input.maxLength = 256; foreign.input.autocomplete = "off";
  const alias = field("给这个友邻的称呼", "例如：港湾的爱丽丝"); alias.input.maxLength = 80;
  const pair = el("button", "primary", "确认配对"); pair.type = "submit";
  form.append(el("h3", "", "或者，使用对方给你的配对码"), address.wrap, foreign.wrap, alias.wrap, pair);
  pairBody.append(form, el("p", "muted", "串门默认关闭。请在 AstrBot 的小世界插件配置中开启串门并保存、重载。默认监听端口 38471；使用域名时，将域名反向代理到本机该端口。配对码与 AstrBot 管理密钥相互独立。"));
  pairing.append(pairBody);
  settingsBody.append(lives, connections, pairing, listenerInfo); settings.append(settingsBody);
  const history = el("section", "visits-history"); history.setAttribute("aria-label", "通话与相见记录");
  const diagnostics = el("details", "visits-diagnostics");
  diagnostics.append(el("summary", "", "联系诊断"));
  const diagnosticBody = el("div", "visits-diagnostics-body"); diagnostics.append(diagnosticBody);
  root.classList.add("visits-journal");
  root.append(heading, notice, peers, phone, current, history, diagnostics, settings);

  function clearSecrets() {
    own.input.value = foreign.input.value = ""; ownInfo.textContent = "";
    secretRevision += 1; clearTimeout(secretTimer);
    if (!root.classList.contains("active")) clearTimeout(refreshTimer);
  }
  function waitingForVisit() {
    return Boolean(lifeData?.active && !lifeData.active.arrived_at && WAITING.has(lifeData.active.status));
  }
  function controls() {
    const enabled = data?.enabled === true;
    const canInvite = enabled && Boolean(getSession()) && lifeData?.scope_enabled === true;
    generate.disabled = pair.disabled = busy || !enabled;
    home.disabled = busy || !enabled || !getSession() || !lifeData?.active;
    const waiting = waitingForVisit();
    home.textContent = waiting ? "取消邀请" : "告别归家";
    refresh.disabled = busy;
    const historyMore = history.querySelector(".visits-history-more");
    if (historyMore) historyMore.disabled = busy || !lifeData || lifeSession !== getSession();
    saveScopes.disabled = busy || !enabled || scopeDraft === null;
    for (const checkbox of lifeChoices.querySelectorAll("input")) checkbox.disabled = busy || !enabled;
    for (const button of [...peers.querySelectorAll("button"), ...phone.querySelectorAll("button"), ...connections.querySelectorAll("button")]) {
      const invite = button.dataset.visitInvite === "true";
      const available = (lifeData?.peers || []).some(peer => peer.id === button.dataset.peerId &&
        peer.connection_id === button.dataset.connectionId && peer.enabled !== false);
      button.disabled = busy || (button.dataset.phoneClose !== "true" && !enabled) ||
        (invite && (!canInvite || !available));
    }
    for (const input of phone.querySelectorAll("input, select")) input.disabled = busy || !enabled;
    const hint = phone.querySelector(".visits-phone-hint");
    if (hint) hint.textContent = !getSession() ? "先在上方选择生活，再打电话约见。" : !lifeData?.scope_enabled
      ? "这个生活尚未参与串门，请在「友邻与生活设置」中勾选并保存。" : lifeData?.active
      ? waiting ? "邀请还在等待决定，双方答应后才会相见。" : "她们正在相见。新的安排会接着这次经历考虑。"
      : "先听听对方是否方便，她们答应后才会相见。";
  }
  async function mutation(endpoint, payload = {}, global = false) {
    if (busy || (!global && !getSession())) return null;
    const session = getSession(); busy = true;
    if (global) globalRevision += 1;
    controls();
    try {
      const result = await apiPost(endpoint, global ? payload : {session, ...payload});
      if (endpoint === "visits/scopes") scopeDraft = null;
      if (global || session === getSession()) await reload();
      return result;
    } catch (error) {
      if (global || session === getSession()) toast(error.message || "这次操作未完成");
      return null;
    } finally {busy = false; controls();}
  }
  function recordLogs(visit) {
    const logs = Array.isArray(visit.transcript) ? visit.transcript : Array.isArray(visit.log) ? visit.log : [];
    return logs.filter(item => item && !(item.kind === "activity" && /^开始相处[：:]/.test(item.text || "")));
  }
  function successful(visit) {
    return Boolean(visit?.arrived_at || visit?.contact_success === true);
  }
  function friendName(connection, actor) {
    const name = connection.alias || connection.name;
    return name && !["小世界", "友邻世界"].includes(name) ? name : actor?.name || name || "友邻世界";
  }
  function momentTime(value) {
    const date = recordDate(value), time = el("time", "visits-log-time muted", clockStamp(value));
    if (date) {time.dateTime = date.toISOString(); time.title = stamp(value);}
    return time;
  }
  function momentList(visit, moments) {
    const list = el("ol", "visits-transcript");
    let previousDay = "";
    for (const moment of moments) {
      const item = moment.items[0], identity = moment.identity;
      const date = recordDate(moment.at), day = date?.toLocaleDateString("zh-CN") || "";
      if (previousDay && day && previousDay !== day) {
        const divider = el("li", "visits-day-break", date.toLocaleDateString("zh-CN", {month: "long", day: "numeric"}));
        list.append(divider);
      }
      if (day) previousDay = day;
      const shared = ["chronicle", "result"].includes(moment.category);
      const isReply = item.kind === "contact_reply", isStatus = item.kind === "contact_status";
      const row = el("li", `visits-moment ${shared ? "is-shared" : identity === visit.local ? "is-local" : identity === visit.remote ? "is-remote" : "is-unknown"}${isReply ? " visits-contact-reply" : isStatus ? " visits-contact-status" : ""}`);
      if (identity) row.dataset.actorId = identity.id || identity.actor_id;
      row.dataset.recordKind = item.kind || "legacy";
      if (shared) {
        const margin = el("div", "visits-chronicle-margin"); margin.append(momentTime(moment.at));
        const body = el("div", "visits-moment-body visits-chronicle-body");
        // Arrival is a protocol milestone, never the host's line of dialogue.
        if (item.kind === "arrival") {
          body.append(el("span", "visits-arrival-mark", "✦"), el("span", "visits-arrival-label", "相见"));
          body.setAttribute("aria-label", "双方已接受邀请并到访");
          for (const part of moment.parts) {
            if (!/^双方接受邀请，开始在东道主世界相处[。！!]?\s*$/.test(part.text)) body.append(el("p", "visits-log-chronicle", part.text));
          }
        } else {
          for (const part of moment.parts) body.append(el("p", part.type === "result" ? "visits-log-result" : "visits-log-chronicle", part.text));
        }
        row.append(margin, body); list.append(row); continue;
      }
      const sameName = identity && visit.local?.name && visit.local.name === visit.remote?.name;
      const legacySpeaker = !item.actor_id && (typeof item.speaker === "string" ? item.speaker
        : typeof item.name === "string" ? item.name : "");
      const speaker = sameName ? `${identity === visit.local ? "我" : "对方"}（${identity.name}）` : identity?.name ||
        (legacySpeaker ? `来源未确认 · ${legacySpeaker}` : "来源未确认");
      const statusSpeaker = identity === visit.local ? "本方当前状态" : identity === visit.remote ? "对方当前状态" : "来源未确认的状态";
      const avatar = el("span", "visits-log-avatar", identity?.name?.slice(0, 1) || "·"); avatar.setAttribute("aria-hidden", "true");
      const body = el("div", "visits-moment-body"), head = el("div", "visits-log-head");
      head.append(avatar, el("strong", "", isStatus ? statusSpeaker : speaker));
      if (isReply || isStatus) head.append(el("span", "visits-log-kind", isReply ? "电话答复" : "通话前状态"));
      head.append(momentTime(moment.at));
      for (const part of moment.parts) {
        const style = {speech: "visits-log-say", intent: "visits-log-action", experience: "visits-log-experience",
          result: "visits-log-result", status: "visits-log-status", chronicle: "visits-log-chronicle"}[part.type];
        const paragraph = el("p", style, part.text);
        if (part.type === "intent") paragraph.setAttribute("aria-label", `当时的行动打算：${part.text}`);
        if (part.type === "experience") paragraph.setAttribute("aria-label", `当时的感受：${part.text}`);
        body.append(paragraph);
      }
      row.append(head, body);
      list.append(row);
    }
    return list;
  }
  function transcript(visit, container, diagnostic = false, live = false) {
    const key = `${getSession()}\n${visit.id || "preview"}`, signature = `${visit.local?.id}\n${visit.remote?.id}`;
    let draft = recordDrafts.get(key);
    if (!draft || draft.signature !== signature) {
      draft = {signature, rows: new Map(), olderOpen: false, loading: false, fetched: false, nextBefore: 0};
      recordDrafts.set(key, draft);
    }
    const present = recordLogs(visit);
    const sequenced = present.length && present.every(item => Number.isInteger(item.seq) && item.seq > 0);
    if (sequenced) for (const item of present) draft.rows.set(item.seq, item);
    const sorted = [...draft.rows.values()].sort((a, b) => a.seq - b.seq);
    // The API cursor is a row boundary: seq 97 follows before=96.
    if (!draft.fetched) draft.nextBefore = sorted.length ? Math.max(0, sorted[0].seq - 1)
      : Number.isInteger(visit.next_before) ? visit.next_before : visit.log_offset > 0 ? visit.log_offset : 0;
    const logs = (sorted.length ? sorted : present).filter(item => (diagnostic || item.kind !== "contact_status")
      && !(item.kind === "activity" && /^开始相处[：:]/.test(item.text || "")));
    const moments = recordMoments(visit, logs), split = live ? Math.max(0, moments.length - 8) : 0;
    if (!moments.length && !draft.nextBefore) return;
    if (split || draft.nextBefore) {
      const earlier = el("details", "visits-earlier-moments"); earlier.open = draft.olderOpen || !live;
      earlier.append(el("summary", "", split ? `更早的片刻 · ${split}` : "翻开更早的片刻"));
      earlier.ontoggle = () => {draft.olderOpen = earlier.open;};
      if (draft.nextBefore) {
        const previous = el("button", "ghost small visits-record-page", draft.loading ? "正在翻页…" : "继续往前翻");
        previous.type = "button"; previous.disabled = draft.loading || !getSession();
        previous.onclick = async () => {
          if (draft.loading || !getSession()) return;
          const session = getSession(), before = draft.nextBefore;
          draft.loading = true; previous.disabled = true; previous.textContent = "正在翻页…";
          try {
            const page = await apiPost("visits/record", {session, visit_id: visit.id, before});
            if (session !== getSession() || recordDrafts.get(key) !== draft || page?.id !== visit.id) return;
            if (!Array.isArray(page.log) || !page.log.every(item => item && Number.isInteger(item.seq) && item.seq > 0 && item.seq <= before)) throw new Error("这页记录暂时读不到，请再试一次");
            for (const item of page.log) draft.rows.set(item.seq, item);
            draft.fetched = true;
            draft.nextBefore = Number.isInteger(page.next_before) && page.next_before > 0 && page.next_before < before ? page.next_before : 0;
          } catch (error) {
            if (session === getSession() && recordDrafts.get(key) === draft) toast(error.message || "这页记录暂时读不到");
          } finally {
            draft.loading = false;
            if (session === getSession() && recordDrafts.get(key) === draft && lifeData && root.classList.contains("active")) renderLife(lifeData);
          }
        };
        earlier.append(previous);
      }
      if (split) earlier.append(momentList(visit, moments.slice(0, split)));
      container.append(earlier);
    }
    if (moments.length) container.append(momentList(visit, moments.slice(split)));
  }
  function sceneArt(scene) {
    const namespace = "http://www.w3.org/2000/svg", svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("viewBox", "0 0 360 210"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
    svg.classList.add("visits-scene-art");
    function shape(tag, attributes, parent = svg) {
      const node = document.createElementNS(namespace, tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
      parent.append(node); return node;
    }
    shape("circle", {cx: 266, cy: 57, r: 25, class: "visits-art-moon"});
    shape("circle", {cx: 266, cy: 57, r: 38, class: "visits-art-orbit"});
    shape("path", {d: "M-12 156Q58 66 122 145T259 116T380 162V220H-12Z", class: "visits-art-land"});
    shape("path", {d: "M-10 183Q75 126 142 171T305 165T374 155", class: "visits-art-path"});
    shape("path", {d: "M32 113Q65 73 104 85M246 142Q280 112 328 128", class: "visits-art-trace"});
    for (const [cx, cy, r] of [[59, 47, 2], [168, 36, 1.7], [321, 72, 1.5], [304, 28, 2], [207, 92, 1.3]]) shape("circle", {cx, cy, r, class: "visits-art-spark"});
    const motif = shape("g", {class: "visits-art-motif"});
    const activity = `${scene?.place || ""} ${scene?.activity || ""}`;
    if (/茶|咖啡|厨房|饼干|烘焙/.test(activity)) {
      shape("path", {d: "M128 117h66v22q0 20-33 20t-33-20zM194 121h8q16 0 16 12t-17 13M118 164h88M146 107q-9-11 0-21M171 107q-9-11 0-21"}, motif);
    } else if (/棋|游戏|牌|玩具/.test(activity)) {
      shape("path", {d: "m112 145 54-31 65 36-54 31zM132 135l64 36M150 124l65 35M132 157l54-31M153 168l54-30"}, motif);
      shape("circle", {cx: 165, cy: 123, r: 6}, motif); shape("circle", {cx: 177, cy: 150, r: 6}, motif);
    } else if (/公园|花|森林|散步|山|河|海|湖|街/.test(activity)) {
      shape("path", {d: "M143 168v-55m0 24q-37-4-27-32 29-2 27 32m0 11q37-2 30-31-29-4-30 31M188 166v-36m0 15q26 0 22-22-23-3-22 22"}, motif);
    } else {
      shape("path", {d: "M124 144h82M129 136h72M134 151v22m58-22v22M145 128q-18-11-7-27M184 128q18-11 7-27"}, motif);
    }
    return svg;
  }
  function outingScene(visit) {
    const outing = visit.outing || {};
    const steps = Array.isArray(outing.stages) ? outing.stages : Array.isArray(outing.route) ? outing.route : [];
    const offset = Number.isInteger(outing.route_offset) && outing.route_offset >= 0 ? outing.route_offset : 0;
    const position = Number.isInteger(outing.stage) ? outing.stage : Number.isInteger(outing.stage_index) ? outing.stage_index
      : Number.isInteger(outing.current_index) ? outing.current_index : offset + Math.max(0, steps.length - 1);
    const index = Math.max(0, Math.min(steps.length - 1, position - offset));
    const stage = outing.current_stage && typeof outing.current_stage === "object" ? outing.current_stage
      : outing.current && typeof outing.current === "object" ? outing.current : steps[index] || {};
    return {outing, steps, index, stage, scene: {...(typeof visit.scene === "object" ? visit.scene : {}), ...stage,
      ...(outing.place ? {place: outing.place} : {}), ...(outing.activity ? {activity: outing.activity} : {})}};
  }
  function scenePanel(visit, live = true) {
    const {outing, steps, index, stage, scene} = outingScene(visit);
    const panel = el("div", "visits-shared-scene"), copy = el("div", "visits-scene-copy");
    copy.append(el("span", "visits-scene-kicker", live ? "此刻 · 一起" : "那天 · 一起"));
    const place = scene.place || scene.location || outing.place || visit.remote?.world || "约好的地方";
    const activity = scene.activity || scene.title || outing.activity || "一起待一会儿";
    copy.append(el("h4", "", place), el("p", "visits-scene-activity", activity));
    const names = [visit.local?.name, visit.remote?.name || visit.peer_name].filter(Boolean);
    if (names.length) copy.append(el("p", "visits-companions", names.join("  ×  ")));
    const start = Number(stage.started_at || outing.started_at || visit.arrived_at || 0);
    const end = Number(stage.until || stage.ends_at || outing.stage_until || outing.until || 0);
    const observed = Number(outing.observed_at || visit.observed_at || Date.now() / 1000);
    if (live && end > start && start > 0) {
      const progress = el("div", "visits-time-progress"); progress.setAttribute("role", "progressbar");
      const percent = Math.max(0, Math.min(100, Math.round((observed - start) / (end - start) * 100)));
      progress.setAttribute("aria-label", "当前共同活动的时间"); progress.setAttribute("aria-valuenow", percent);
      progress.setAttribute("aria-valuemin", "0"); progress.setAttribute("aria-valuemax", "100");
      progress.style.setProperty("--visit-progress", `${percent}%`); progress.append(el("span"));
      copy.append(progress, el("p", "visits-scene-clock", `${stamp(start)} — ${stamp(end)}`));
    }
    panel.append(copy, sceneArt(scene));
    if (steps.length > 1) {
      const route = el("ol", "visits-route"); route.setAttribute("aria-label", "共同活动路线");
      const first = Math.max(0, index - 7);
      for (const [offset, item] of steps.slice(first, first + 8).entries()) {
        const position = first + offset;
        const point = el("li", !live || position < index ? "is-past" : position === index ? "is-current" : "");
        if (live && position === index) point.setAttribute("aria-current", "step");
        point.append(el("span", "visits-route-dot"), el("span", "", item.place || item.location || item.activity || item.title || "约好的活动"));
        route.append(point);
      }
      panel.append(route);
    }
    return panel;
  }
  function visitCard(visit, live = false, diagnostic = false, expanded = false) {
    const card = el(live ? "div" : "details", "visits-visit");
    const place = visit.arrived_at && (visit.outing?.place || visit.outing_version !== 1 && visit.scene?.place);
    const friend = visit.remote?.name || visit.peer_name || visit.peer?.name || "友邻世界";
    const keepsake = !live && !diagnostic;
    const label = keepsake ? [friend, place].filter(Boolean).join(" · ") : `${friend} · ${STATUS[visit.status] || visit.status || "等待答复"}`;
    const summary = el(live ? "h3" : "summary", keepsake ? "visits-entry-summary" : "", keepsake ? "" : label);
    if (!live) {
      card.dataset.visitId = visit.id;
      const start = visit.arrived_at || visit.started_at || visit.created_at, date = recordDate(start);
      if (keepsake) {
        const day = el("span", "visits-entry-date"); day.setAttribute("aria-hidden", "true");
        day.append(el("b", "", date ? String(date.getDate()).padStart(2, "0") : "·"),
          el("span", "", date ? `${date.getMonth() + 1}月` : "札记"));
        const copy = el("span", "visits-entry-copy"); copy.append(el("strong", "visits-entry-title", label));
        const firstSpeech = recordLogs(visit).filter(item => item.kind !== "invitation")
          .flatMap(recordParts).find(part => part.type === "speech");
        const preview = firstSpeech?.text || (visit.arrived_at && visit.outing?.activity) || "";
        if (preview) copy.append(el("span", "visits-entry-preview", preview));
        const interval = stamp(start) + (recordDate(visit.ended_at) && Number(visit.ended_at) >= Number(start)
          ? ` — ${date?.toDateString() === recordDate(visit.ended_at).toDateString() ? clockStamp(visit.ended_at) : stamp(visit.ended_at)}` : "");
        if (interval) copy.append(el("small", "visits-history-time muted", interval));
        const arrow = el("span", "visits-entry-arrow", "↗"); arrow.setAttribute("aria-hidden", "true");
        summary.append(day, copy, arrow);
      } else if (date) summary.append(el("small", "visits-history-time muted", stamp(start)));
    }
    card.append(summary);
    const body = el("div", "visits-visit-body"), scene = visit.scene;
    let bodyReady = false;
    function populateBody() {
      if (bodyReady) return;
      bodyReady = true;
      const contactNotes = [typeof visit.contact_error === "string" ? visit.contact_error.trim() : "", visit.model_wait?.reason].filter(Boolean);
      if (live && contactNotes.length) {
        const waiting = el("details", "visits-contact-notes"); waiting.append(el("summary", "", "联系状态"));
        for (const note of contactNotes) waiting.append(el("p", "visits-channel-status muted", note));
        body.append(waiting);
      }
      const endReason = typeof visit.end_reason === "string" ? visit.end_reason.trim() : "";
      if (diagnostic) {
        const ending = el("div", "visits-end-reason");
        ending.append(el("p", "", endReason || "这条联系没有留下具体说明。"));
        body.append(ending);
      }
      const sceneText = typeof scene === "string" ? scene : [scene?.place, scene?.activity].filter(value => typeof value === "string").join(" · ");
      const awaitingActivity = visit.arrived_at && visit.outing_version === 1 && !visit.outing?.place;
      if (awaitingActivity) body.append(el("p", "visits-scene", live ? "已经见面，接下来一起做什么还在商量。" : "那次见面还没有共同确认游玩安排。"));
      else if (visit.arrived_at && (live || visit.outing?.place)) body.append(scenePanel(visit, live));
      else if (sceneText) {
        const description = el("p", visit.arrived_at ? "visits-scene" : "visits-scene-plan");
        if (live && !visit.arrived_at) description.append(el("span", "visits-scene-kicker", "约见打算 · "));
        description.append(document.createTextNode(sceneText)); body.append(description);
      }
      if (visit.reason && diagnostic) body.append(el("p", "muted", visit.reason));
      transcript(visit, body, diagnostic || live && !visit.arrived_at, live);
    }
    if (live || diagnostic || expanded) populateBody();
    else card.ontoggle = () => {if (card.open) populateBody();};
    if (expanded && !live) card.open = true;
    card.append(body); return card;
  }
  function renderGlobal(result) {
    data = result; peers.replaceChildren(); lifeChoices.replaceChildren(); connections.replaceChildren();
    const listener = result.listener || {port: result.listen_port, public_url: result.public_url, error: result.error};
    notice.hidden = result.enabled === true && !listener.error;
    notice.textContent = listener.error || (!result.enabled ? "串门尚未开启 · 在插件配置中开启后即可联系友邻" : "");
    listenerInfo.textContent = `串门地址：${listener.public_url || `${listener.host || "0.0.0.0"}:${listener.port || 38471}`}`;
    settingsSummary.textContent = `友邻与生活设置 · ${(result.connections || []).length} 个友邻 / ${(result.enabled_scopes || []).length} 项生活`;
    const peersHead = el("div", "visits-section-head");
    peersHead.append(el("h3", "", "认识的友邻"), el("span", "muted", `${(result.connections || []).length} 个世界`));
    peers.append(peersHead);
    connections.append(el("h3", "", "已配对的友邻"));
    const list = el("div", "visits-peer-grid");
    for (const connection of result.connections || []) {
      const actors = (connection.actors || []).filter(actor => actor && typeof actor.id === "string" && actor.id);
      const draft = friendDrafts.get(connection.id) || {actorId: "", reason: ""};
      if (!actors.some(actor => actor.id === draft.actorId)) draft.actorId = actors[0]?.id || "";
      friendDrafts.set(connection.id, draft);
      const card = el("article", "visits-peer card"); card.dataset.connectionId = connection.id;
      const actor = actors.find(item => item.id === draft.actorId);
      const identity = el("div", "visits-peer-identity");
      const name = friendName(connection, actor);
      const avatar = el("span", "visits-peer-avatar", (actor?.name || name).slice(0, 1)); avatar.setAttribute("aria-hidden", "true");
      const description = el("div", "visits-peer-description");
      description.append(el("h4", "", name), el("p", "visits-actor muted", actor
        ? [actor.name !== name ? actor.name : "", actor.world].filter(Boolean).join(" · ") : "等待对方的生活"));
      identity.append(avatar, description); card.append(identity);
      const footer = el("div", "visits-peer-footer"), call = el("button", "ghost small", "打电话"); call.type = "button";
      call.setAttribute("aria-controls", phone.id); call.setAttribute("aria-expanded", String(phoneConnection === connection.id));
      call.onclick = () => {
        phoneConnection = phoneConnection === connection.id ? null : connection.id;
        renderPhone();
        if (!phone.hidden) phone.scrollIntoView({block: "nearest", behavior: "smooth"});
      };
      footer.append(el("span", "visits-peer-count muted", actors.length > 1 ? `${actors.length} 位朋友` : actors.length ? "已认识" : "等待对方开启生活"), call);
      const meetings = (result.history || result.visits || []).filter(visit => successful(visit)
        && (visit.connection_id === connection.id || actors.some(item => item.id === visit.remote?.id)));
      if (meetings.length) footer.firstChild.textContent = `${meetings.length} 次相见`;
      card.append(footer); card.classList.toggle("selected", phoneConnection === connection.id); list.append(card);
      const settingRow = el("div", "visits-connection-row"), settingInfo = el("div", "visits-connection-info");
      settingInfo.append(el("strong", "", name), el("p", "visits-address muted", connection.url || ""));
      if (connection.catalog_error) settingInfo.append(el("p", "visits-catalog-error muted", connection.catalog_error));
      const actions = el("div", "toolbar");
      const catalog = el("button", "ghost small", "刷新角色"); catalog.type = "button";
      catalog.onclick = () => mutation("visits/refresh", {connection_id: connection.id}, true); actions.append(catalog);
      const remove = el("button", "ghost small", "解除配对"); remove.type = "button";
      remove.onclick = async () => {
        if (confirm && !await confirm({title: "解除友邻配对", message: `解除与「${connection.alias || connection.name || "这个世界"}」的全局配对？所有生活都会停止与这家友邻的新邀请，已有记忆会保留。`, confirmText: "解除配对"})) return;
        await mutation("visits/remove", {connection_id: connection.id}, true);
      }; actions.append(remove); settingRow.append(settingInfo, actions); connections.append(settingRow);
    }
    if (!list.children.length) list.append(el("p", "muted", "还没有友邻。先与另一台服务器全局配对，再勾选参与的生活。"));
    peers.append(list);
    if (phoneConnection && !(result.connections || []).some(connection => connection.id === phoneConnection)) phoneConnection = null;
    renderPhone();
    const selected = scopeDraft === null ? new Set(result.enabled_scopes || []) : scopeDraft;
    for (const life of result.lives || []) {
      const row = el("label", "visits-life-choice"), checkbox = el("input"); checkbox.type = "checkbox"; checkbox.value = life.scope;
      checkbox.checked = selected.has(life.scope); checkbox.setAttribute("aria-label", life.label || life.scope);
      checkbox.onchange = () => {
        if (scopeDraft === null) scopeDraft = new Set(result.enabled_scopes || []);
        if (checkbox.checked) scopeDraft.add(life.scope); else scopeDraft.delete(life.scope);
        controls();
      };
      row.append(checkbox, el("span", "", life.label || life.scope)); lifeChoices.append(row);
    }
    if (!lifeChoices.children.length) lifeChoices.append(el("p", "muted", "请先创建并启用一个生活，再在这里选择参与串门。"));
    controls();
  }
  function renderPhone() {
    phone.replaceChildren();
    const connection = (data?.connections || []).find(item => item.id === phoneConnection);
    phone.hidden = !connection;
    for (const card of peers.querySelectorAll(".visits-peer")) {
      const selected = card.dataset.connectionId === phoneConnection; card.classList.toggle("selected", selected);
      card.querySelector("button").setAttribute("aria-expanded", String(selected));
    }
    if (!connection) return;
    const actors = (connection.actors || []).filter(actor => actor && typeof actor.id === "string" && actor.id);
    const draft = friendDrafts.get(connection.id), head = el("div", "visits-phone-head");
    const close = el("button", "ghost small", "收起"); close.type = "button"; close.dataset.phoneClose = "true";
    close.onclick = () => {phoneConnection = null; renderPhone();};
    const actor = actors.find(item => item.id === draft.actorId);
    const phoneTitle = el("h3", "", `打给 ${friendName(connection, actor)}`);
    head.append(phoneTitle, close); phone.append(head);
    const body = el("div", "visits-phone-body");
    if (actors.length > 1) {
      const wrap = el("label", "field"), selector = el("select"); selector.setAttribute("aria-label", "去见哪位朋友");
      for (const actor of actors) {const option = el("option", "", [actor.name || "朋友", actor.world].filter(Boolean).join(" · ")); option.value = actor.id; selector.append(option);}
      selector.value = draft.actorId; wrap.append(el("span", "", "去见哪位朋友"), selector); body.append(wrap);
      selector.onchange = () => {
        draft.actorId = selector.value;
        const card = [...peers.querySelectorAll(".visits-peer")].find(item => item.dataset.connectionId === connection.id);
        const actor = actors.find(item => item.id === draft.actorId);
        const name = friendName(connection, actor); card.querySelector("h4").textContent = name;
        phoneTitle.textContent = `打给 ${name}`;
        card.querySelector(".visits-actor").textContent = [actor.name !== name ? actor.name : "", actor.world].filter(Boolean).join(" · ");
        card.querySelector(".visits-peer-avatar").textContent = (actor.name || "朋友").slice(0, 1);
        for (const button of phone.querySelectorAll('[data-visit-invite="true"]')) button.dataset.peerId = draft.actorId;
        controls();
      };
    }
    if (!actors.length) phone.append(el("p", "muted", "对方还没有开启参与串门的生活，可在设置中刷新角色。"));
    const reason = field("这次邀请想一起做什么", "可选，例如一起下完昨天那盘棋"); reason.input.maxLength = 300;
    reason.input.value = draft.reason; reason.input.oninput = () => {draft.reason = reason.input.value;}; body.append(reason.wrap);
    const actions = el("div", "toolbar visits-phone-actions");
    for (const [direction, label] of [["outgoing", "去做客"], ["incoming", "请她来"]]) {
      const button = el("button", direction === "outgoing" ? "primary small" : "ghost small", label); button.type = "button";
      button.dataset.visitInvite = "true"; button.dataset.connectionId = connection.id; button.dataset.peerId = draft.actorId;
      button.onclick = async () => {
        const reply = await mutation("visits/invite", {peer_id: button.dataset.peerId, direction, reason: reason.input.value.trim()});
        if (reply) toast(STATUS[reply.status] || "电话已打出，等待她们决定");
      }; actions.append(button);
    }
    body.append(actions); phone.append(body, el("p", "visits-phone-hint muted")); controls();
  }
  function renderLife(result) {
    const opened = new Set([...history.querySelectorAll("details[open]")].map(card => card.dataset.visitId));
    const diagnosticOpened = new Set([...diagnosticBody.querySelectorAll("details[open]")].map(card => card.dataset.visitId));
    lifeData = result; current.replaceChildren(); history.replaceChildren(); diagnosticBody.replaceChildren();
    current.classList.toggle("is-home", !result.active);
    current.classList.toggle("is-waiting", Boolean(result.active && !result.active.arrived_at));
    if (getSession() && result.active) current.append(visitCard(result.active, true));
    else {
      current.append(el("span", "visits-home-mark", "⌂"), el("span", "muted", !getSession()
        ? "选择一份生活，翻开她的相见札记" : result.scope_enabled ? "此刻在自己的世界" : "这份生活尚未参与串门"));
    }
    const all = result.history || result.visits || [];
    const past = all.filter(item => item.id !== result.active?.id && successful(item));
    const historyHead = el("div", "visits-section-head");
    historyHead.append(el("h3", "", "通话与相见记录"), el("span", "muted", past.length ? `${past.length} 篇札记` : "")); history.append(historyHead);
    for (const visit of past.slice(0, historyLimit)) {
      history.append(visitCard(visit, false, false, opened.has(visit.id)));
    }
    if (past.length > historyLimit) {
      const session = lifeSession;
      const more = el("button", "ghost visits-history-more", "继续翻阅"); more.type = "button";
      more.onclick = () => {
        if (!lifeData || session !== getSession() || session !== lifeSession) return;
        historyLimit += 20; renderLife(lifeData);
      }; history.append(more);
    }
    if (!past.length) {
      const empty = el("div", "visits-empty-note"); empty.append(el("span", "", "✧"), el("span", "muted", "相见之后，这里会留下她们的札记。")); history.append(empty);
    }
    const failed = [...(Array.isArray(result.diagnostics) ? result.diagnostics : []), ...all.filter(item => !successful(item)
      && item.id !== result.active?.id)].filter((item, index, items) => item?.id && items.findIndex(other => other.id === item.id) === index);
    const budget = result.automatic_budget;
    const peerBudget = (result.peers || []).filter(peer => peer.availability?.allowed === false);
    const hasBudgetNotes = budget?.allowed === false || peerBudget.length > 0;
    diagnostics.hidden = !failed.length && !hasBudgetNotes;
    diagnostics.querySelector("summary").textContent = `联系诊断${failed.length ? ` · ${failed.length}` : ""}`;
    if (budget?.allowed === false) diagnosticBody.append(el("p", "visits-budget-note", `本方自动联系：${budget.reason || "今天的自动额度已用完"}`));
    for (const peer of peerBudget) diagnosticBody.append(el("p", "visits-budget-note", `${peer.name || "对方"}的自动联系：${peer.availability.reason || "今天的自动额度已用完"}`));
    for (const visit of failed.slice(0, 20)) {
      const card = visitCard(visit, false, true); card.open = diagnosticOpened.has(visit.id); diagnosticBody.append(card);
    }
    controls();
    clearTimeout(refreshTimer);
    if (result.active && root.classList.contains("active")) refreshTimer = setTimeout(() => {
      if (root.classList.contains("active")) reload();
    }, 15000);
  }
  async function reload() {
    const session = getSession(), request = ++revision, globalRequest = globalRevision;
    if (session !== lifeSession) {
      recordDrafts.clear(); historyLimit = 20; lifeSession = session; renderLife({scope_enabled: false, active: null, history: []});
    }
    lifeData = null; controls();
    try {
      const result = await apiGet("visits", session ? {session} : {});
      if (globalRequest === globalRevision && request >= renderedGlobalRead) {renderedGlobalRead = request; renderGlobal(result);}
      if (request === revision && session === getSession()) renderLife(result);
    } catch (error) {
      if (request === revision && session === getSession()) {
        notice.hidden = false;
        notice.textContent = error.message || "暂时读不到串门状态";
      }
    }
  }
  refresh.onclick = reload;
  saveScopes.onclick = () => mutation("visits/scopes", {sessions: [...(scopeDraft || [])]}, true);
  home.onclick = async () => {
    const waiting = waitingForVisit();
    if (await mutation("visits/return")) toast(waiting ? "这次邀请已取消" : "这次串门准备结束，保留已确认的经历");
  };
  generate.onclick = async () => {
    clearSecrets(); const secretRequest = secretRevision;
    const result = await mutation("visits/pair-code", {}, true);
    if (!result || secretRequest !== secretRevision || !root.classList.contains("active")) return;
    own.input.value = result.code || "";
    ownInfo.textContent = [result.url ? `本机地址：${result.url}` : "将本机公网地址或已配置的域名交给对方。",
      result.expires_at ? `有效至 ${stamp(result.expires_at)}；仅可使用一次。` : "仅可使用一次。"].join(" ");
    own.input.focus(); own.input.select();
    if (result.expires_at) secretTimer = setTimeout(clearSecrets, Math.max(0, Number(result.expires_at) * 1000 - Date.now()));
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const code = foreign.input.value.trim(); foreign.input.value = "";
    const result = await mutation("visits/pair", {url: address.input.value.trim(), code, alias: alias.input.value.trim()}, true);
    if (result) {clearSecrets(); pairing.open = false; toast("已全局配对，勾选参与的生活后就可以提出邀请");}
  };
  controls();
  return {reload, clearSecrets};
}
