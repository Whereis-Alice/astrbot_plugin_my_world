/** Paired worlds use their own channel; the editor only invites and observes. */
import {recordCursor, recordIdentity, recordMoments, recordParts} from "./visit-records.js";

const STATUS = {
  invited: "邀请已提出", inviting: "正在提出邀请", offered: "等待接待决定", pending: "等待答复", accepted: "对方已接受",
  visiting: "正在做客", active: "相见中", arrived: "已经到访", returning: "正在告别归家",
  finishing: "正在告别归家", completed: "已告别归家", closed: "已经结束", ended: "已经结束", declined: "这次没有成行",
  rejected: "对方暂时不方便", cancelled: "邀请已取消", interrupted: "这次暂时中断",
  expired: "这次已结束", failed: "这次没有成行",
};
const WAITING = new Set(["invited", "inviting", "offered", "pending", "accepted"]);
const ENDED = new Set(["completed", "closed", "ended", "declined", "rejected", "cancelled", "interrupted", "expired", "failed"]);
const OPEN = new Set([...WAITING, "visiting", "active", "arrived", "returning", "finishing"]);

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

export function createVisitsPage({root, apiGet, apiPost, el, toast, getSession, confirm, download}) {
  let revision = 0, globalRevision = 0, renderedGlobalRead = 0, secretRevision = 0, busy = false;
  let data = null, lifeData = null, lifeSession = null, refreshTimer, secretTimer, scopeDraft = null, phoneConnection = null;
  let cardDraft = null, cardFeedback = "", cardSaving = false;
  let statusDraft = null, statusFeedback = "", statusSaving = false;
  const friendDrafts = new Map();
  const recordDrafts = new Map();
  const cardExports = new Map();
  let exportEpoch = 0;
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
  const recordCards = el("section", "visits-card-settings"); recordCards.setAttribute("aria-label", "相见长卡片设置");
  const cardHeading = el("div", "visits-card-settings-head"), cardMark = el("span", "visits-card-settings-mark", "✧");
  cardMark.setAttribute("aria-hidden", "true");
  const cardIntro = el("div"); cardIntro.append(el("h3", "", "把相见寄到聊天里"),
    el("p", "muted", "相见结束后，将已确认的经过排成清晰长图，发到勾选会话。优先本地渲染，失败自动回退。"));
  cardHeading.append(cardMark, cardIntro);
  const cardToggleRow = el("label", "visits-card-toggle"), cardToggle = el("input"); cardToggle.type = "checkbox";
  cardToggle.setAttribute("aria-label", "自动发送相见长卡片");
  cardToggleRow.append(cardToggle, el("span", "", "自动发送相见长卡片"));
  const cardChoices = el("div", "visits-card-choices"); cardChoices.setAttribute("aria-label", "接收长卡片的会话");
  const cardNote = el("p", "visits-card-note muted", "只发送开启后结束的新相见记录，历史记录不补发。共享生活的会话也需分别勾选；只收到自己所属生活的记录。");
  const cardFooter = el("div", "visits-card-settings-footer"), saveCards = el("button", "primary", "保存长卡片设置");
  saveCards.type = "button";
  const cardStatus = el("p", "visits-card-status muted"); cardStatus.setAttribute("role", "status");
  cardFooter.append(saveCards, cardStatus); recordCards.append(cardHeading, cardToggleRow, cardChoices, cardNote, cardFooter);
  const statusCards = el("section", "visits-status-settings visits-card-settings");
  statusCards.setAttribute("aria-label", "串门群名片状态设置");
  statusCards.append(el("h3", "", "串门时，名片跟着行动"),
    el("p", "muted", "赶路、泡茶、下棋或等待同伴时，显示当前行动的短状态。结束后恢复正常名片。"));
  const statusToggle = el("input"); statusToggle.type = "checkbox";
  const statusToggleRow = el("label", "visits-card-toggle");
  statusToggle.setAttribute("aria-label", "串门名片随行动更新");
  statusToggleRow.append(statusToggle, el("span", "", "随当前行动更新"));
  const statusModel = el("input"); statusModel.type = "checkbox";
  const statusModelRow = el("label", "visits-card-toggle");
  statusModel.setAttribute("aria-label", "用模型生成串门短状态");
  statusModelRow.append(statusModel, el("span", "", "用模型生成自然的短状态"));
  const statusProviderRow = el("label", "field"), statusProvider = el("select");
  statusProvider.setAttribute("aria-label", "串门名片状态模型");
  statusProviderRow.append(el("span", "", "状态模型"), statusProvider);
  const statusLength = field("状态最多字数", "12", "number");
  statusLength.input.min = "6"; statusLength.input.max = "20"; statusLength.input.step = "1";
  const saveStatus = el("button", "primary", "保存名片状态设置"); saveStatus.type = "button";
  const statusFeedbackLine = el("p", "visits-card-status muted"); statusFeedbackLine.setAttribute("role", "status");
  const statusFooter = el("div", "visits-card-settings-footer"); statusFooter.append(saveStatus, statusFeedbackLine);
  statusCards.append(statusToggleRow, statusModelRow, statusProviderRow, statusLength.wrap,
    el("p", "muted", "留空跟随会话默认模型。只在行动或阶段变化时生成；等待或失败时先显示行动短词。"), statusFooter);
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
  settingsBody.append(recordCards, statusCards, lives, connections, pairing, listenerInfo); settings.append(settingsBody);
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
  function clearCardExports() {
    exportEpoch += 1;
    for (const state of cardExports.values()) clearTimeout(state.timer);
    cardExports.clear();
  }
  function cardExportPanel(visit) {
    const session = getSession(), key = `${session}\0${visit.id}`;
    let state = cardExports.get(key);
    if (!state) {
      state = {phase: "idle", image: null, pageData: null, pageRequest: 0, epoch: exportEpoch};
      cardExports.set(key, state);
    }
    const panel = el("section", "visits-export"); panel.setAttribute("aria-label", "本条记录的长卡片");
    const current = () => cardExports.get(key) === state && state.epoch === exportEpoch && getSession() === session;
    const params = () => ({session, visit_id: visit.id, job_id: state.job_id});
    const redraw = () => {if (current()) state.draw();};
    async function loadPage() {
      if (!current() || state.phase !== "ready" || state.pageData) return;
      const request = ++state.pageRequest;
      state.pageLoading = true; state.pageError = ""; state.pageData = null; redraw();
      try {
        const result = await apiGet("visits/card-export-page", {...params(), page: 0});
        if (!current() || request !== state.pageRequest) return;
        if (typeof result.data_url !== "string" || !/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+$/.test(result.data_url)) {
          throw new Error("预览图片暂时不可用，请重试");
        }
        state.pageData = {data_url: result.data_url};
      } catch (error) {
        if (current() && request === state.pageRequest) state.pageError = error.message || "预览图片加载失败";
      } finally {
        if (current() && request === state.pageRequest) {state.pageLoading = false; redraw();}
      }
    }
    function acceptJob(result) {
      if (!result || typeof result.job_id !== "string" || !result.job_id) throw new Error("暂时无法读取卡片生成进度，请重试");
      state.job_id = result.job_id;
      if (result.state === "failed") throw new Error(result.error || "卡片生成失败，请重试");
      if (result.state === "ready") {
        if (!Array.isArray(result.pages) || result.pages.length !== 1) throw new Error("卡片结果不是完整长图，请重新生成");
        state.phase = "ready"; state.image = result.pages[0];
        state.pageData = null; state.pageError = "";
        redraw(); void loadPage(); return;
      }
      if (result.state !== "pending") throw new Error("卡片状态暂时不可用，请重试");
      if (Date.now() - state.started > 180000) throw new Error("生成时间较长，请稍后重试查看进度");
      state.phase = "pending"; redraw();
      state.timer = setTimeout(async () => {
        if (!current()) return;
        try {
          const status = await apiGet("visits/card-export-status", params());
          if (current()) acceptJob(status);
        } catch (error) {
          if (current()) {state.phase = "failed"; state.error = error.message || "暂时读不到生成进度，请重试"; redraw();}
        }
      }, 1000);
    }
    async function generateCard() {
      if (!current() || state.phase === "pending") return;
      clearTimeout(state.timer); state.pageRequest += 1;
      Object.assign(state, {phase: "pending", image: null, pageData: null, pageLoading: false,
        error: "", pageError: "", downloadError: "", started: Date.now(), zoom: false});
      redraw();
      try {
        const result = await apiPost("visits/card-export", {session, visit_id: visit.id});
        if (current()) acceptJob(result);
      } catch (error) {
        if (current()) {state.phase = "failed"; state.error = error.message || "卡片生成失败，请重试"; redraw();}
      }
    }
    async function downloadCard() {
      if (!current() || state.downloading || state.phase !== "ready" || typeof download !== "function") return;
      state.downloading = true; state.downloadError = ""; redraw();
      try {
        await download("visits/card-export-download", {...params(), page: 0},
          state.image?.filename || "my-world-visit-card.png");
      } catch (error) {
        if (current()) state.downloadError = error.message || "下载失败，请重试";
      } finally {
        if (current()) {state.downloading = false; redraw();}
      }
    }
    state.draw = () => {
      panel.replaceChildren();
      const head = el("div", "visits-export-head"), copy = el("div", "visits-export-copy");
      copy.append(el("strong", "", "把这次相见留下来"), el("p", "muted", "完整记录生成一张长图，在这里预览或下载。仅保存到本地，不发送到 QQ。"));
      const generate = el("button", "ghost small visits-export-generate", state.phase === "pending" ? "正在生成…" : state.phase === "ready" ? "重新生成" : "生成卡片");
      generate.type = "button"; generate.disabled = state.phase === "pending"; generate.onclick = generateCard;
      head.append(copy, generate); panel.append(head);
      const status = el("p", "visits-export-status muted"); status.setAttribute("role", "status");
      if (state.phase === "pending") status.textContent = "正在整理完整记录并生成高清卡片，可以继续查看记录。";
      else if (state.phase === "failed") {status.textContent = state.error; status.classList.add("is-error");}
      else if (state.phase === "ready") status.textContent = "已生成完整长图 · 原始清晰度下载";
      if (status.textContent) panel.append(status);
      if (state.phase !== "ready") return;
      const toolbar = el("div", "visits-export-toolbar");
      const actions = el("div", "visits-export-downloads"), save = el("button", "ghost small", "下载图片");
      save.type = "button"; save.disabled = state.downloading || typeof download !== "function"; save.onclick = () => downloadCard();
      actions.append(save);
      toolbar.append(actions); panel.append(toolbar);
      if (state.downloadError) {const error = el("p", "visits-export-status is-error", state.downloadError); error.setAttribute("role", "status"); panel.append(error);}
      if (state.pageLoading) panel.append(el("p", "visits-export-status muted", "正在加载预览…"));
      else if (state.pageError) {
        const error = el("div", "visits-export-preview-error"); error.append(el("p", "visits-export-status is-error", state.pageError));
        const retry = el("button", "ghost small", "重试预览"); retry.type = "button"; retry.onclick = loadPage; error.append(retry); panel.append(error);
      } else if (state.pageData) {
        const previewHead = el("div", "visits-export-preview-head"); previewHead.append(el("span", "muted", "卡片预览 · 可向下滚动"));
        const zoom = el("button", "ghost small", state.zoom ? "适应宽度" : "放大预览"); zoom.type = "button";
        zoom.setAttribute("aria-pressed", String(Boolean(state.zoom))); zoom.onclick = () => {state.zoom = !state.zoom; redraw();}; previewHead.append(zoom);
        const preview = el("div", "visits-export-preview" + (state.zoom ? " is-zoomed" : "")); preview.tabIndex = 0;
        preview.setAttribute("aria-label", "完整相见长卡片预览");
        const image = el("img", "visits-export-image"); image.src = state.pageData.data_url;
        image.alt = "本次相见的完整长卡片";
        preview.append(image); panel.append(previewHead, preview);
      }
    };
    state.draw(); return panel;
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
    saveCards.disabled = busy || cardDraft === null || !data?.record_card;
    for (const input of recordCards.querySelectorAll("input")) input.disabled = busy || !data?.record_card;
    cardStatus.textContent = cardSaving ? "正在保存…" : cardDraft !== null
      ? cardFeedback || "有未保存的更改" : cardFeedback || (data?.record_card?.enabled
        ? `已开启 · ${data.record_card.sessions?.length || 0} 个接收会话` : "默认关闭 · 由你选择是否寄出");
    saveStatus.disabled = busy || statusDraft === null || !data?.card_status;
    for (const input of statusCards.querySelectorAll("input, select")) input.disabled = busy || !data?.card_status;
    statusProvider.disabled ||= !statusModel.checked;
    statusFeedbackLine.textContent = statusSaving ? "正在保存…" : statusFeedback || (statusDraft !== null
      ? "有未保存的更改" : data?.card_status?.enabled ? "已开启 · 跟随当前行动" : "只显示接待或拜访");
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
    cardSaving = endpoint === "visits/record-card-settings";
    statusSaving = endpoint === "visits/card-status-settings";
    if (global) globalRevision += 1;
    controls();
    try {
      const result = await apiPost(endpoint, global ? payload : {session, ...payload});
      if (endpoint === "visits/scopes") scopeDraft = null;
      if (endpoint === "visits/record-card-settings") {
        cardDraft = null; cardFeedback = "已保存，立即生效";
        data.record_card = result; renderRecordCards();
      }
      if (endpoint === "visits/card-status-settings") {
        statusDraft = null; statusFeedback = "已保存，立即生效";
        data.card_status = result; renderCardStatus();
      }
      if (global || session === getSession()) await reload();
      return result;
    } catch (error) {
      if (endpoint === "visits/record-card-settings") cardFeedback = "保存失败，当前选择已保留";
      if (endpoint === "visits/card-status-settings") statusFeedback = "保存失败，当前选择已保留";
      if (global || session === getSession()) toast(error.message || "这次操作未完成");
      return null;
    } finally {busy = false; cardSaving = false; statusSaving = false; controls();}
  }
  function editCardStatus() {
    statusDraft = {enabled: statusToggle.checked, model_enabled: statusModel.checked,
      provider_id: statusProvider.value, max_length: Number(statusLength.input.value)};
    statusFeedback = ""; controls();
  }
  function renderCardStatus() {
    const config = data?.card_status || {enabled: true, model_enabled: true, provider_id: "", max_length: 12};
    const value = statusDraft || config;
    statusToggle.checked = value.enabled === true; statusModel.checked = value.model_enabled === true;
    statusLength.input.value = String(value.max_length);
    statusProvider.replaceChildren();
    const choices = [{id: "", label: "跟随 AstrBot 会话默认模型"}, ...(config.providers || [])];
    if (value.provider_id && !choices.some(row => row.id === value.provider_id))
      choices.push({id: value.provider_id, label: `${value.provider_id}（当前未加载）`});
    for (const choice of choices) {
      const option = el("option", "", choice.id ? `${choice.label} · ${choice.id}` : choice.label);
      option.value = choice.id; statusProvider.append(option);
    }
    statusProvider.value = value.provider_id || "";
  }
  for (const input of [statusToggle, statusModel, statusProvider, statusLength.input]) input.onchange = editCardStatus;
  function editRecordCards() {
    if (cardDraft === null) cardDraft = {enabled: data?.record_card?.enabled === true,
      sessions: new Set(data?.record_card?.sessions || [])};
    cardFeedback = "";
    return cardDraft;
  }
  function renderRecordCards() {
    const config = data?.record_card || {enabled: false, sessions: [], choices: []};
    const selected = cardDraft?.sessions || new Set(config.sessions || []);
    cardToggle.checked = cardDraft ? cardDraft.enabled : config.enabled === true;
    cardChoices.replaceChildren();
    const choices = Array.isArray(config.choices) ? [...config.choices] : [];
    const known = new Set(choices.map(item => item.session_id));
    for (const id of selected) if (!known.has(id)) choices.push({session_id: id, label: "已移除的会话，请取消勾选", enabled: false});
    for (const choice of choices) {
      const row = el("label", "visits-card-choice"), checkbox = el("input");
      checkbox.type = "checkbox"; checkbox.value = choice.session_id; checkbox.checked = selected.has(choice.session_id);
      checkbox.setAttribute("aria-label", `接收相见长卡片：${choice.label || choice.session_id}`);
      checkbox.onchange = () => {
        const draft = editRecordCards();
        if (checkbox.checked) draft.sessions.add(choice.session_id); else draft.sessions.delete(choice.session_id);
        controls();
      };
      const description = el("span", "visits-card-choice-copy");
      description.append(el("span", "", choice.label || choice.session_id),
        el("small", "muted", `${choice.session_id}${choice.enabled === false ? " · 会话已停用" : ""}`));
      row.append(checkbox, description); cardChoices.append(row);
    }
    if (!choices.length) cardChoices.append(el("p", "muted", "先在「会话」中创建接收记录的群聊或私聊，再回来勾选。"));
  }
  cardToggle.onchange = () => {editRecordCards().enabled = cardToggle.checked; controls();};
  function recordRows(visit) {
    const logs = Array.isArray(visit.log) && visit.log.length ? visit.log
      : Array.isArray(visit.transcript) ? visit.transcript : [];
    return logs.filter(item => item && typeof item === "object");
  }
  function recordLogs(visit) {
    return recordRows(visit).filter(item => !(item.kind === "activity" && /^开始相处[：:]/.test(item.text || "")));
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
      draft = {signature, rows: new Map(), olderOpen: false, loading: false, nextBefore: 0};
      recordDrafts.set(key, draft);
    }
    const present = recordRows(visit);
    const sequenced = present.length && present.every(item => Number.isInteger(item.seq) && item.seq > 0);
    if (sequenced) for (const item of present) draft.rows.set(item.seq, item);
    const sorted = [...draft.rows.values()].sort((a, b) => a.seq - b.seq);
    // Keep protocol rows, including hidden legacy annotations, when looking for gaps.
    draft.nextBefore = recordCursor(sorted, visit);
    const logs = (sorted.length ? sorted : present).filter(item => (diagnostic || item.kind !== "contact_status")
      && !(item.kind === "activity" && /^开始相处[：:]/.test(item.text || "")));
    const moments = recordMoments(visit, logs), split = live ? Math.max(0, moments.length - 8) : 0;
    if (!moments.length && !draft.nextBefore) return;
    if (split || draft.nextBefore) {
      const earlier = el("details", "visits-earlier-moments"); earlier.open = draft.olderOpen || !live;
      earlier.append(el("summary", "", split ? `更早的片刻 · ${split}` : "翻开更早的片刻"));
      earlier.ontoggle = () => {draft.olderOpen = earlier.open;};
      if (draft.nextBefore) {
        const note = el("p", "visits-record-gap", sorted[0]?.seq === 1
          ? "这里还有一段未加载的经过，继续往前翻可补齐。" : "还有更早的经过未加载。");
        earlier.append(note);
        const previous = el("button", "ghost small visits-record-page", draft.loading ? "正在翻页…" : "继续往前翻");
        previous.type = "button"; previous.disabled = draft.loading || !getSession();
        previous.onclick = async () => {
          if (draft.loading || !getSession()) return;
          const session = getSession(), before = draft.nextBefore;
          draft.loading = true; previous.disabled = true; previous.textContent = "正在翻页…";
          try {
            const page = await apiPost("visits/record", {session, visit_id: visit.id, before});
            if (session !== getSession() || recordDrafts.get(key) !== draft || page?.id !== visit.id) return;
            if (!Array.isArray(page.log) || !page.log.length || !page.log.every(item => item && Number.isInteger(item.seq) && item.seq > 0 && item.seq <= before)
                || !page.log.some(item => !draft.rows.has(item.seq))) throw new Error("这页记录暂时读不到，请再试一次");
            for (const item of page.log) draft.rows.set(item.seq, item);
            draft.nextBefore = recordCursor([...draft.rows.values()], page);
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
    const observed = Number(outing.observed_at || visit.observed_at || Date.now() / 1000);
    const traveling = live && Number(outing.travel_seconds) > 0 && Number(outing.started_at) > observed;
    const destination = scene.place || scene.location || outing.place || visit.remote?.world || "约好的地方";
    const place = traveling ? `${outing.origin_place || "路上"} → ${destination}` : destination;
    const activity = traveling ? "正在一起赶路" : scene.activity || scene.title || outing.activity || "一起待一会儿";
    copy.append(el("h4", "", place), el("p", "visits-scene-activity", activity));
    const names = [visit.local?.name, visit.remote?.name || visit.peer_name].filter(Boolean);
    if (names.length) copy.append(el("p", "visits-companions", names.join("  ×  ")));
    const start = traveling ? Number(outing.started_at) - Number(outing.travel_seconds)
      : Number(stage.started_at || outing.started_at || visit.arrived_at || 0);
    const end = traveling ? Number(outing.started_at) : Number(stage.until || stage.ends_at || outing.stage_until || outing.until || 0);
    if (live && end > start && start > 0) {
      const progress = el("div", "visits-time-progress"); progress.setAttribute("role", "progressbar");
      const percent = Math.max(0, Math.min(100, Math.round((observed - start) / (end - start) * 100)));
      progress.setAttribute("aria-label", traveling ? "当前共同路途的时间" : "当前共同活动的时间"); progress.setAttribute("aria-valuenow", percent);
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
  function sharedBrief(visit, live) {
    const shared = visit.shared_activity || {}, brief = el("section", "visits-brief");
    brief.setAttribute("aria-label", live ? "这次相处的简短进展" : "这次相处的短纪要");
    const text = value => typeof value === "string" ? value.trim() : "";
    const summary = text(shared.summary), moments = Array.isArray(shared.milestones) ? shared.milestones : [];
    const seen = new Set();
    brief.append(el("h4", "", live ? "一起做到了这里" : "这次记住的事"));
    if (summary) {brief.append(el("p", "visits-brief-summary", summary)); seen.add(summary);}
    const recent = moments.filter(row => row && text(row.summary) && !seen.has(text(row.summary))).slice(-3);
    if (recent.length) {
      const list = el("ol", "visits-brief-milestones");
      for (const row of recent) {
        const item = el("li"); item.append(momentTime(row.at), el("span", "", text(row.summary))); list.append(item);
      }
      brief.append(list);
    }
    const pending = Array.isArray(shared.pending) ? shared.pending.filter(value => text(value)) : [];
    if (pending.length) brief.append(el("p", "visits-brief-pending", `${live ? "还没做完" : "当时未完成"}：${pending.slice(-4).join("；")}`));
    if (!summary && !recent.length && !pending.length) {
      const result = recordLogs(visit).filter(row => row.kind === "result" || row.kind === "outcome").at(-1);
      if (text(result?.text)) brief.append(el("p", "visits-brief-summary", text(result.text)));
      else return null;
    }
    return brief;
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
        const preview = visit.shared_activity?.summary || visit.shared_activity?.milestones?.at(-1)?.summary
          || firstSpeech?.text || (visit.arrived_at && visit.outing?.activity) || "";
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
      if (!live && ENDED.has(visit.status) && visit.id) body.append(cardExportPanel(visit));
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
      if (visit.arrived_at && !diagnostic) {
        const brief = sharedBrief(visit, live);
        if (brief) body.append(brief);
        const raw = el("details", "visits-full-transcript");
        raw.append(el("summary", "", "完整原话与经过"));
        transcript(visit, raw, false, live);
        const recordKey = `${getSession()}\n${visit.id || "preview"}`;
        raw.open = recordDrafts.get(recordKey)?.fullOpen === true;
        raw.ontoggle = () => {const draft = recordDrafts.get(recordKey); if (draft) draft.fullOpen = raw.open;};
        body.append(raw);
      } else transcript(visit, body, diagnostic || live && !visit.arrived_at, live);
      const progress = visit.progress;
      if (live && progress && typeof progress.reason === "string" && progress.reason.trim()) {
        const continuation = el("footer", "visits-record-progress");
        continuation.dataset.progressKind = typeof progress.kind === "string" ? progress.kind : "";
        const speaker = progress.kind === "next_turn" ? recordIdentity(visit, {actor_id: progress.actor_id}) : null;
        const sameName = speaker && visit.local?.name === visit.remote?.name;
        const name = speaker && (sameName ? `${speaker === visit.local ? "本方" : "对方"}的${speaker.name}` : speaker.name);
        continuation.append(el("span", "visits-record-progress-mark", "⋯"),
          el("span", "visits-progress-reason", name ? `接下来由${name}回应` : progress.reason));
        if (recordDate(progress.next_at) && Number(progress.next_at) > Number(visit.observed_at || Date.now() / 1000)) {
          const expected = el("span", "visits-progress-time", `约 ${clockStamp(progress.next_at)} 继续`);
          expected.title = stamp(progress.next_at); continuation.append(expected);
        }
        body.append(continuation);
      } else if (!live && !diagnostic) {
        const ending = el("footer", "visits-record-ending");
        ending.dataset.status = visit.status || "unknown";
        const ended = ENDED.has(visit.status) || recordDate(visit.ended_at);
        const heading = el("div", "visits-record-ending-head");
        heading.append(el("span", "visits-record-ending-mark", ended ? "✧" : "⋯"),
          el("span", "", STATUS[visit.status] || (ended ? "已结束，原状态未保存" : "后续状态未保存")));
        if (recordDate(visit.ended_at)) heading.append(momentTime(visit.ended_at));
        ending.append(heading);
        const parts = recordLogs(visit).flatMap(recordParts);
        const alreadyRecorded = parts.at(-1)?.text.trim() === endReason
          || parts.findLast(part => part.type === "speech")?.text.trim() === endReason;
        if (endReason && !alreadyRecorded) ending.append(el("p", "visits-record-ending-reason", endReason));
        else if (!endReason) ending.append(el("p", "visits-record-ending-reason", ended
          ? "这份旧记录没有保存结束说明。" : OPEN.has(visit.status) ? "记录到这里仍未结束。" : "这份旧记录没有保存后续状态。"));
        body.append(ending);
      }
    }
    if (live || diagnostic || expanded) populateBody();
    else card.ontoggle = () => {if (card.open) populateBody();};
    if (expanded && !live) card.open = true;
    card.append(body); return card;
  }
  function belongsToConnection(visit, connection) {
    return visit.connection_id === connection.id || visit.remote?.instance_id === connection.id
      || (connection.actors || []).some(actor => actor?.id && actor.id === visit.remote?.id);
  }
  function contactActors(connection) {
    const actors = (connection.actors || []).filter(actor => actor && typeof actor.id === "string" && actor.id);
    if (!data?.scope_routing || !getSession()) return actors;
    const matched = new Set((data.peers || []).filter(peer => peer.connection_id === connection.id).map(peer => peer.id));
    return actors.filter(actor => matched.has(actor.id));
  }
  function renderGlobal(result) {
    data = result; peers.replaceChildren(); lifeChoices.replaceChildren(); connections.replaceChildren();
    renderRecordCards();
    renderCardStatus();
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
      const actors = contactActors(connection);
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
      footer.append(el("span", "visits-peer-count muted", actors.length > 1 ? `${actors.length} 位朋友` : actors.length ? "已认识"
        : data?.scope_routing && getSession() ? "当前会话暂无对应生活" : "等待对方开启生活"), call);
      const meetings = (result.history || result.visits || []).filter(visit => successful(visit)
        && belongsToConnection(visit, connection));
      if (meetings.length) footer.firstChild.textContent = `${meetings.length} 次相见`;
      footer.firstChild.title = `${getSession() ? "当前生活" : "本实例"}保留的相见记录，含正在进行的相见`;
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
    const actors = contactActors(connection);
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
    if (!actors.length) phone.append(el("p", "muted", data?.scope_routing && getSession()
      ? "尚未确认对方在当前群聊中的生活。请确认双方已更新并开启这个会话的串门，再刷新角色。"
      : "对方还没有开启参与串门的生活，可在设置中刷新角色。"));
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
    const previousFriends = past.filter(visit => !(result.connections || []).some(connection => belongsToConnection(visit, connection)));
    if (previousFriends.length) history.append(el("p", "visits-history-note muted",
      `其中 ${previousFriends.length} 篇来自当前友邻列表以外的旧友邻，仍保留在这里。`));
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
      clearCardExports();
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
  saveCards.onclick = async () => {
    if (cardDraft === null) return;
    await mutation("visits/record-card-settings", {enabled: cardDraft.enabled, sessions: [...cardDraft.sessions]}, true);
  };
  saveStatus.onclick = async () => {
    if (statusDraft === null) return;
    if (!Number.isInteger(statusDraft.max_length) || statusDraft.max_length < 6 || statusDraft.max_length > 20) {
      statusFeedback = "状态长度请填写6～20字"; controls(); return;
    }
    await mutation("visits/card-status-settings", statusDraft, true);
  };
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
