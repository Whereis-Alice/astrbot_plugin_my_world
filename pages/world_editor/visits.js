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
  let revision = 0, renderedSession = "", busy = false, data = null, refreshTimer;
  const heading = el("div", "visits-heading");
  const title = el("div"); title.append(el("span", "people-eyebrow", "友邻世界"),
    el("h2", "", "先打个招呼，再去朋友家坐坐"),
    el("p", "muted", "各自带着自己的生活相见。邀请、聊天和共同经历留在这里，回来后也会记得。"));
  const toolbar = el("div", "toolbar"), refresh = el("button", "ghost", "刷新"), home = el("button", "ghost", "告别归家");
  refresh.type = home.type = "button"; toolbar.append(refresh, home); heading.append(title, toolbar);
  const notice = el("p", "visits-notice muted"); notice.setAttribute("role", "status");
  const current = el("section", "visits-current card"); current.setAttribute("aria-label", "当前串门");
  const peers = el("section", "visits-peers"); peers.setAttribute("aria-label", "友邻世界列表");
  const pairing = el("details", "visits-pairing card");
  pairing.append(el("summary", "", "添加一个友邻世界"));
  const pairBody = el("div", "visits-pair-body"), generate = el("button", "ghost", "生成一次性配对码"); generate.type = "button";
  pairBody.append(el("h3", "", "把这边的世界介绍给对方"),
    el("p", "muted", "选好上方生活会话后生成配对码，把本机串门地址和配对码交给对方管理员。对方只需填一次，双方就会成为友邻。"), generate);
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
  root.append(heading, notice, current, peers, pairing, history);

  function clearSecrets() {
    own.input.value = foreign.input.value = ""; ownInfo.textContent = "";
    clearTimeout(refreshTimer);
  }
  function controls() {
    const enabled = data?.enabled === true && Boolean(getSession());
    generate.disabled = pair.disabled = busy || !enabled;
    home.disabled = busy || !enabled || !data?.active;
    refresh.disabled = busy || !getSession();
    for (const button of peers.querySelectorAll("button")) button.disabled = busy || !enabled;
  }
  async function mutation(endpoint, payload = {}) {
    if (busy || !getSession()) return null;
    const session = getSession(); busy = true; controls();
    try {
      const result = await apiPost(endpoint, {session, ...payload});
      if (session === getSession()) await reload();
      return result;
    } catch (error) {
      if (session === getSession()) toast(error.message || "这次操作未完成");
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
  function render(result) {
    data = result; current.replaceChildren(); peers.replaceChildren(); history.replaceChildren();
    const listener = result.listener || {port: result.listen_port, public_url: result.public_url, error: result.error};
    notice.textContent = result.enabled
      ? `已开启串门 · ${listener.public_url || `监听 ${listener.host || "0.0.0.0"}:${listener.port || 38471}`}。空闲时不持续联系其他服务器；来访由双方自行决定。`
      : "串门尚未开启。打开插件配置中的串门开关、保存并重载后，可以在这里配对友邻。";
    if (listener.error) notice.textContent += ` 服务状态：${listener.error}`;
    current.append(el("span", "people-eyebrow", "此刻"));
    current.append(result.active ? visitCard(result.active, true) : el("p", "muted", "她正在自己的世界生活，目前没有串门。"));
    peers.append(el("h3", "", "认识的友邻"));
    const list = el("div", "visits-peer-grid");
    for (const peer of result.peers || []) {
      const card = el("article", "visits-peer card"), actor = peer.actor || {};
      card.append(el("h4", "", peer.alias || peer.name || actor.name || "友邻"),
        el("p", "muted", peer.world || actor.world || ""), el("p", "visits-address muted", peer.url || ""));
      const reason = field("这次邀请想一起做什么", "可选，例如一起下完昨天那盘棋"); reason.input.maxLength = 300;
      const actions = el("div", "toolbar");
      for (const [direction, label] of [["outgoing", "邀请去做客"], ["incoming", "邀请她来访"]]) {
        const button = el("button", "ghost small", label); button.type = "button";
        button.onclick = async () => {
          const reply = await mutation("visits/invite", {peer_id: peer.id, direction, reason: reason.input.value.trim()});
          if (reply) toast(STATUS[reply.status] || "邀请已提出，等待她们决定");
        }; actions.append(button);
      }
      const remove = el("button", "ghost small", "解除配对"); remove.type = "button";
      remove.onclick = async () => {
        if (confirm && !await confirm({title: "解除友邻配对", message: `解除与「${peer.alias || peer.name || actor.name || "这个世界"}」的配对？已发生的经历仍会保留。`, confirmText: "解除配对"})) return;
        await mutation("visits/remove", {peer_id: peer.id});
      }; actions.append(remove); card.append(reason.wrap, actions); list.append(card);
    }
    if (!list.children.length) list.append(el("p", "muted", "还没有友邻。先与另一台服务器上的世界配对，再提出邀请。"));
    peers.append(list); history.append(el("h3", "", "见过面的日子"));
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
    const session = getSession(), request = ++revision;
    if (session !== renderedSession) {clearSecrets(); renderedSession = session;}
    if (!session) {
      data = null; current.replaceChildren(); peers.replaceChildren(); history.replaceChildren();
      notice.textContent = "请先在上方选择一个已启用的生活会话。"; controls(); return;
    }
    try {
      const result = await apiGet("visits", {session});
      if (request !== revision || session !== getSession()) return;
      render(result);
    } catch (error) {
      if (request === revision && session === getSession()) notice.textContent = error.message || "暂时读不到串门状态";
    }
  }
  refresh.onclick = reload;
  home.onclick = async () => {if (await mutation("visits/return")) toast("这次串门准备结束，保留已确认的经历");};
  generate.onclick = async () => {
    clearSecrets(); const session = getSession();
    const result = await mutation("visits/pair-code");
    if (!result || session !== getSession() || !root.classList.contains("active")) return;
    own.input.value = result.code || "";
    ownInfo.textContent = [result.url ? `本机地址：${result.url}` : "将本机公网地址或已配置的域名交给对方。",
      result.expires_at ? `有效至 ${stamp(result.expires_at)}；仅可使用一次。` : "仅可使用一次。"].join(" ");
    own.input.focus(); own.input.select();
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const code = foreign.input.value.trim(); foreign.input.value = "";
    const result = await mutation("visits/pair", {url: address.input.value.trim(), code, alias: alias.input.value.trim()});
    if (result) {pairing.open = false; toast("已成为友邻，现在可以提出邀请");}
  };
  controls();
  return {reload, clearSecrets};
}
