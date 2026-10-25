/** Devices are explicit life options. Reading reuses the bounded recent-message archive. */
const READING = {window_hours: 12, max_messages: 500, max_chars: 60000};

export function openBrowseSettings({world, sessions = [], scopeLabel = "全局默认", el, openCustomDialog, markDirty}) {
  let read;
  openCustomDialog({title: "手机与电脑", confirmText: "应用设置",
    hint: `当前编辑范围：${scopeLabel}。应用后点击顶栏「保存」写入配置。`,
    build: body => {read = browseSettings({world, sessions, el, body});},
    onSubmit: () => {world.chat_browse = read(); markDirty(); return true;}});
}

export function browseSettings({world, sessions = [], el, body}) {
  const draft = {enabled: false, phone: false, computer_nodes: [], sessions: [], cooldown_minutes: 120,
    daily_max: 3, ...READING, remember_meaningful: true, reply_enabled: false,
    reply_wait_seconds: 120, relationship_context: true, ...world.chat_browse};
  const readers = [], inputs = {};
  const panel = el("div", "browse-settings");
  const intro = el("div", "browse-intro");
  intro.append(el("span", "people-eyebrow", "安静翻消息"), el("h3", "", "看看消息，也留一点自己的想法"),
    el("p", "muted", "她可以在空闲时看看近期消息，然后继续自己的生活。提供的设备应符合原有人设；默认安静阅读，感想单独留在「内心活动 → 翻消息时」。"));
  panel.append(intro);
  const content = el("div", "browse-settings-content");
  function toggle(parent, key, label) {
    const field = el("label", "browse-toggle"), input = el("input");
    input.type = "checkbox"; input.checked = Boolean(draft[key]);
    field.append(input, el("span", "", label)); parent.append(field);
    readers.push(() => {draft[key] = input.checked;});
  }
  const devices = el("div", "browse-device-toggles");
  toggle(devices, "enabled", "空闲时可以安静翻消息");
  toggle(devices, "phone", "她有一部手机（随身可用）"); content.append(devices);
  function choices(key, title, options, hint) {
    const group = el("fieldset", "browse-choices"); group.append(el("legend", "", title), el("p", "muted", hint));
    const list = el("div", "browse-choice-list");
    const known = new Set(options.map(item => item.value));
    for (const value of draft[key] || []) if (!known.has(value)) options.push({value, label: value});
    const choices = options.map(({value, label}) => {
      const field = el("label", "browse-toggle"), input = el("input"); input.type = "checkbox";
      input.value = value; input.checked = (draft[key] || []).includes(value);
      field.append(input, el("span", "", label)); list.append(field); return input;
    });
    if (!options.length) list.append(el("p", "muted", "暂无可选项目"));
    group.append(list); content.append(group);
    readers.push(() => {draft[key] = choices.filter(input => input.checked).map(input => input.value);});
  }
  choices("computer_nodes", "哪些地点有电脑", (world.nodes || []).map(node => ({value: node.id, label: node.name || node.id})), "不选就不看电脑消息。电脑只在所选地点可用，无需服务器电脑或沙盒。");
  choices("sessions", "可以翻阅哪些会话", sessions.map(item => ({value: item.session_id, label: item.label || item.note || item.group_name || item.session_id})), "留空使用同一生活组内已启用的会话。每次只读一个会话，感想留在来源会话。");
  function numbers(parent, fields) {
    const grid = el("div", "browse-limits");
    for (const [key, label, min, max, unit] of fields) {
      const field = el("label", "field"), input = el("input"); input.type = "number"; input.required = true;
      input.setAttribute("aria-label", label);
      input.min = min; input.max = max; input.step = 1; input.value = draft[key]; inputs[key] = input;
      field.append(el("span", "", label), input, el("small", "muted", `${min.toLocaleString()}–${max.toLocaleString()} ${unit}`)); grid.append(field);
      readers.push(() => {if (!input.checkValidity()) throw new Error(`${label}需为 ${min}–${max} 之间的整数`); draft[key] = Number(input.value);});
    }
    parent.append(grid);
  }
  const reading = el("section", "browse-reading"), heading = el("div", "browse-section-head");
  heading.append(el("h3", "", "每次读多少"));
  const reset = el("button", "ghost small", "恢复推荐量"); reset.type = "button";
  reset.onclick = () => {for (const [key, value] of Object.entries(READING)) inputs[key].value = value;};
  heading.append(reset); reading.append(heading);
  numbers(reading, [["window_hours", "消息范围（小时）", 1, 24, "小时"],
    ["max_messages", "每次最多读取条数", 1, 1000, "条"], ["max_chars", "每次正文最多字数", 300, 120000, "字"]]);
  reading.append(el("p", "browse-reading-note muted", "推荐最近 12 小时 · 最多 500 条 · 60,000 字。条数和字数同时生效，取先到的上限；只读插件已收到且仍在缓存内的文字，不向平台补拉历史。"));
  content.insertBefore(reading, content.querySelector(".browse-choices"));
  const cadence = el("section", "browse-cadence"); cadence.append(el("h3", "", "多久翻一次"));
  numbers(cadence, [["cooldown_minutes", "两次浏览至少间隔（分钟）", 30, 1440, "分钟"], ["daily_max", "每天最多浏览次数", 1, 100, "次"]]);
  cadence.append(el("p", "muted", "这是最短间隔。时间到了，会等下一个有新消息、设备可用的空闲时刻；不会打断睡眠或手头的事。次数按开始浏览计算，中断或想法生成失败也占用一次；成功读完会记录想法。"));
  content.append(cadence);
  const reply = el("section", "browse-reply");
  reply.append(el("h3", "", "看完，也可以接一句"));
  toggle(reply, "reply_enabled", "浏览后允许她判断是否接话");
  numbers(reply, [["reply_wait_seconds", "短暂忙碌最多等待（秒）", 0, 300, "秒"]]);
  reply.append(el("p", "muted", "默认关闭。每次成功浏览都会在「内心活动 → 翻消息时」留下一点想法。开启后由她自己决定是否接话，可以选择回应哪条消息，也可以看完继续生活；只接来源会话里仍在进行的话题。发言还需在全局主动发言白名单中获准，群聊和私聊均可。"));
  reply.append(el("p", "muted", "遇到正在回复或活动收尾时，短暂等待后重新看现场；原消息已回应、已判断、话题结束或等待超时就放下。设为 0 不等待；等待不重读、不强制发言。"));
  content.append(reply);
  const memory = el("div", "browse-memory");
  toggle(memory, "relationship_context", "阅读时参考已有关系与兴趣简述");
  memory.append(el("p", "muted", "只参考来源会话实际作者的已有资料，最多 6 人、1,200 字；画像总开关关闭时不加入。不会额外提取画像或改变好感度。"));
  toggle(memory, "remember_meaningful", "有持续意义的感受可留下生活记忆");
  memory.append(el("p", "muted", "普通感想保留 24 小时；有意义的感受保留来源，不成为群聊摘要。共享生活组共用次数与冷却，每次阅读仍服从后台预算。读取量越大，单次模型输入消耗越多。"));
  content.append(memory); panel.append(content); body.append(panel);
  return () => {readers.forEach(read => read()); return draft;};
}
