/** Paired worlds use their own channel; the editor only invites and observes. */
const STATUS = {
  invited: "邀请已提出", inviting: "正在提出邀请", pending: "等待答复", accepted: "对方已接受",
  visiting: "正在做客", active: "正在一起活动", arrived: "已经到访", returning: "正在告别归家",
  completed: "已告别归家", closed: "已经结束", ended: "已经结束", declined: "这次没有成行",
  rejected: "对方暂时不方便", cancelled: "邀请已取消", interrupted: "这次暂时中断",
  expired: "这次已结束", failed: "这次没有成行",
};

function stamp(value) {
  if (!value) return "";
  const date = new Date(Number(value) * (Number(value) < 1e12 ? 1000 : 1));
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
}

export function createVisitsPage({root, apiGet, apiPost, el, toast, getSession, confirm}) {
  let revision = 0, globalRevision = 0, renderedGlobalRead = 0, secretRevision = 0, busy = false;
  let data = null, lifeData = null, lifeSession = null, refreshTimer, secretTimer, scopeDraft = null;
  const friendDrafts = new Map();
  const heading = el("div", "visits-heading");
  const title = el("div"); title.append(el("span", "people-eyebrow", "友邻世界"),
    el("h2", "", "先打个招呼，再去朋友家坐坐"),
    el("p", "muted", "各自带着自己的生活相见。邀请、聊天和共同经历留在这里，回来后也会记得。"));
  const toolbar = el("div", "toolbar"), refresh = el("button", "ghost", "刷新"), home = el("button", "ghost", "告别归家");
  refresh.type = home.type = "button"; toolbar.append(refresh, home); heading.append(title, toolbar);
  const notice = el("p", "visits-notice muted"); notice.setAttribute("role", "status");
  const current = el("section", "visits-current card"); current.setAttribute("aria-label", "当前串门");
  const peers = el("section", "visits-peers"); peers.setAttribute("aria-label", "友邻世界列表");
  const lives = el("section", "visits-lives card"); lives.setAttribute("aria-label", "参与串门的生活");
  const lifeChoices = el("div", "visits-life-choices");
  const saveScopes = el("button", "primary", "保存参与生活"); saveScopes.type = "button";
  lives.append(el("h3", "", "哪些生活参与串门"),
    el("p", "muted", "友邻全局配对一次，再勾选参与的生活。共享生活组只需勾选一项；各自的经历与记忆仍留在自己的生活里。"), lifeChoices, saveScopes);
  const pairing = el("details", "visits-pairing card");
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
  const history = el("section", "visits-history"); history.setAttribute("aria-label", "过去的串门");
  root.append(heading, notice, peers, pairing, lives, current, history);

  function clearSecrets() {
    own.input.value = foreign.input.value = ""; ownInfo.textContent = "";
    secretRevision += 1; clearTimeout(secretTimer); clearTimeout(refreshTimer);
  }
  function controls() {
    const enabled = data?.enabled === true;
    const canInvite = enabled && Boolean(getSession()) && lifeData?.scope_enabled === true;
    generate.disabled = pair.disabled = busy || !enabled;
    home.disabled = busy || !enabled || !getSession() || !lifeData?.active;
    refresh.disabled = busy;
    saveScopes.disabled = busy || !enabled || scopeDraft === null;
    for (const checkbox of lifeChoices.querySelectorAll("input")) checkbox.disabled = busy || !enabled;
    for (const button of peers.querySelectorAll("button")) {
      const invite = button.dataset.visitInvite === "true";
      const available = (lifeData?.peers || []).some(peer => peer.id === button.dataset.peerId &&
        peer.connection_id === button.dataset.connectionId && peer.enabled !== false);
      button.disabled = busy || !enabled || (invite && (!canInvite || !available || Boolean(lifeData?.active)));
    }
    for (const select of peers.querySelectorAll("select")) select.disabled = busy || !enabled;
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
  function transcript(visit, container) {
    const logs = Array.isArray(visit.transcript) ? visit.transcript : Array.isArray(visit.log) ? visit.log : [];
    if (!logs.length) {
      container.append(el("p", "muted", "还没有已确认的共同经过。邀请成功后才会开始到访。")); return;
    }
    const list = el("ol", "visits-transcript");
    for (const item of logs.slice(-30)) {
      const identity = [visit.local, visit.remote].find(actor => actor &&
        (actor.id === item.actor_id || actor.actor_id === item.actor_id));
      const row = el("li"), speaker = typeof item.speaker === "string" ? item.speaker : item.name || identity?.name || "共同经过";
      const when = stamp(item.at); row.append(el("strong", "", speaker), el("small", "muted", when));
      for (const text of [item.say || item.text, item.action, item.result].filter(value => typeof value === "string" && value.trim())) row.append(el("p", "", text));
      list.append(row);
    }
    container.append(list);
  }
  function visitCard(visit, live = false) {
    const card = el(live ? "div" : "details", "visits-visit");
    const label = `${visit.peer_name || visit.peer?.name || visit.remote?.name || "友邻世界"} · ${STATUS[visit.status] || visit.status || "等待答复"}`;
    card.append(el(live ? "h3" : "summary", "", label));
    const body = el("div", "visits-visit-body"), scene = visit.scene;
    const sceneText = typeof scene === "string" ? scene : [scene?.place, scene?.activity].filter(value => typeof value === "string").join(" · ");
    if (sceneText) body.append(el("p", "visits-scene", sceneText));
    if (visit.reason) body.append(el("p", "muted", visit.reason));
    const metrics = [stamp(visit.started_at || visit.created_at),
      typeof (visit.call_count ?? visit.calls_used) === "number" ? `已用 ${visit.call_count ?? visit.calls_used} 次模型请求` : "",
      live && visit.expires_at ? `最晚 ${stamp(visit.expires_at)} 结束` : ""].filter(Boolean);
    if (metrics.length) body.append(el("p", "muted", metrics.join(" · ")));
    transcript(visit, body); card.append(body); return card;
  }
  function renderGlobal(result) {
    data = result; peers.replaceChildren(); lifeChoices.replaceChildren();
    const listener = result.listener || {port: result.listen_port, public_url: result.public_url, error: result.error};
    notice.textContent = result.enabled
      ? `已开启串门 · ${listener.public_url || `监听 ${listener.host || "0.0.0.0"}:${listener.port || 38471}`}。每个生活每天可向每家友邻自动发起一次成功到访；没有成行时稍后再试。`
      : "串门尚未开启。打开插件配置中的串门开关、保存并重载后，可以在这里配对友邻。";
    if (listener.error) notice.textContent += ` 服务状态：${listener.error}`;
    peers.append(el("h3", "", "认识的友邻"));
    const list = el("div", "visits-peer-grid");
    for (const connection of result.connections || []) {
      const actors = (connection.actors || []).filter(actor => actor && typeof actor.id === "string" && actor.id);
      const draft = friendDrafts.get(connection.id) || {actorId: "", reason: ""};
      if (!actors.some(actor => actor.id === draft.actorId)) draft.actorId = actors[0]?.id || "";
      friendDrafts.set(connection.id, draft);
      const card = el("article", "visits-peer card"); card.dataset.connectionId = connection.id;
      card.append(el("h4", "", connection.alias || connection.name || "友邻世界"),
        el("p", "visits-address muted", connection.url || ""));
      if (actors.length === 1) card.append(el("p", "visits-actor", [actors[0].name, actors[0].world].filter(Boolean).join(" · ")));
      let selector;
      if (actors.length > 1) {
        const wrap = el("label", "field"); selector = el("select"); selector.setAttribute("aria-label", "去见哪位朋友");
        for (const actor of actors) {
          const option = el("option", "", [actor.name || "朋友", actor.world].filter(Boolean).join(" · "));
          option.value = actor.id; selector.append(option);
        }
        selector.value = draft.actorId; wrap.append(el("span", "", "去见哪位朋友"), selector); card.append(wrap);
      }
      if (!actors.length) card.append(el("p", "muted", "对方还没有开启参与串门的生活，可在对方保存后刷新角色。"));
      if (connection.catalog_error) card.append(el("p", "visits-catalog-error muted", connection.catalog_error));
      const reason = field("这次邀请想一起做什么", "可选，例如一起下完昨天那盘棋"); reason.input.maxLength = 300;
      reason.input.value = draft.reason; reason.input.oninput = () => {draft.reason = reason.input.value;};
      const actions = el("div", "toolbar");
      for (const [direction, label] of [["outgoing", "邀请去做客"], ["incoming", "邀请她来访"]]) {
        const button = el("button", "ghost small", label); button.type = "button";
        button.dataset.visitInvite = "true"; button.dataset.connectionId = connection.id; button.dataset.peerId = draft.actorId;
        button.onclick = async () => {
          const reply = await mutation("visits/invite", {peer_id: button.dataset.peerId, direction, reason: reason.input.value.trim()});
          if (reply) toast(STATUS[reply.status] || "邀请已提出，等待她们决定");
        }; actions.append(button);
      }
      if (selector) selector.onchange = () => {
        draft.actorId = selector.value;
        for (const button of actions.querySelectorAll('[data-visit-invite="true"]')) button.dataset.peerId = draft.actorId;
        controls();
      };
      const catalog = el("button", "ghost small", "刷新角色"); catalog.type = "button";
      catalog.onclick = () => mutation("visits/refresh", {connection_id: connection.id}, true); actions.append(catalog);
      const remove = el("button", "ghost small", "解除配对"); remove.type = "button";
      remove.onclick = async () => {
        if (confirm && !await confirm({title: "解除友邻配对", message: `解除与「${connection.alias || connection.name || "这个世界"}」的全局配对？所有生活都会停止与这家友邻的新邀请，已有记忆会保留。`, confirmText: "解除配对"})) return;
        await mutation("visits/remove", {connection_id: connection.id}, true);
      }; actions.append(remove); card.append(reason.wrap, actions); list.append(card);
    }
    if (!list.children.length) list.append(el("p", "muted", "还没有友邻。先与另一台服务器全局配对，再勾选参与的生活。"));
    peers.append(list);
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
  function renderLife(result) {
    lifeData = result; current.replaceChildren(); history.replaceChildren();
    current.append(el("span", "people-eyebrow", "当前生活"));
    if (!getSession()) current.append(el("p", "muted", "在上方选择生活后，可以查看她的串门经历并提出邀请。"));
    else if (result.active) current.append(visitCard(result.active, true));
    else current.append(el("p", "muted", result.scope_enabled
      ? "她正在自己的世界生活，目前没有串门。"
      : "这个生活尚未参与串门。在上方勾选并保存后，即可与已配对的友邻相见。"));
    history.append(el("h3", "", "见过面的日子"));
    const past = result.history || result.visits || [];
    for (const visit of past.filter(item => item.id !== result.active?.id).slice(0, 20)) history.append(visitCard(visit));
    if (!history.querySelector("details")) history.append(el("p", "muted", "共同经历会在这里留下记录，也会成为她自己的生活记忆。"));
    controls();
    clearTimeout(refreshTimer);
    if (result.active && root.classList.contains("active")) refreshTimer = setTimeout(() => {
      if (root.classList.contains("active")) reload();
    }, 15000);
  }
  async function reload() {
    const session = getSession(), request = ++revision, globalRequest = globalRevision;
    if (session !== lifeSession) {
      lifeSession = session; renderLife({scope_enabled: false, active: null, history: []});
    }
    lifeData = null; controls();
    try {
      const result = await apiGet("visits", session ? {session} : {});
      if (globalRequest === globalRevision && request >= renderedGlobalRead) {renderedGlobalRead = request; renderGlobal(result);}
      if (request === revision && session === getSession()) renderLife(result);
    } catch (error) {
      if (request === revision && session === getSession()) notice.textContent = error.message || "暂时读不到串门状态";
    }
  }
  refresh.onclick = reload;
  saveScopes.onclick = () => mutation("visits/scopes", {sessions: [...(scopeDraft || [])]}, true);
  home.onclick = async () => {if (await mutation("visits/return")) toast("这次串门准备结束，保留已确认的经历");};
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
