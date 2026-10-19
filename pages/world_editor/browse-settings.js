/** Quiet reading is a life option, with explicit devices and bounded input. */
export function browseSettings({world, sessions = [], el, body}) {
  const draft = {enabled: false, phone: false, computer_nodes: [], sessions: [], cooldown_minutes: 120,
    daily_max: 3, window_hours: 6, max_messages: 20, max_chars: 3000, remember_meaningful: true,
    ...world.chat_browse};
  const readers = [];
  const panel = el("details", "browse-settings");
  panel.append(el("summary", "", "安静翻消息"));
  panel.append(el("p", "muted", "她可以在空闲时翻翻手机或电脑里的近期消息，留下一点自己的感受。这个活动不会主动发消息。默认关闭；设备需要你明确提供，并应符合原有人设。"));
  const content = el("div", "browse-settings-content");
  function toggle(key, label) {
    const field = el("label", "browse-toggle"), input = el("input");
    input.type = "checkbox"; input.checked = Boolean(draft[key]);
    field.append(input, el("span", "", label)); content.append(field);
    readers.push(() => {draft[key] = input.checked;});
  }
  toggle("enabled", "空闲时可以安静翻消息");
  toggle("phone", "她有一部手机（随身可用）");
  function choices(key, title, options, hint) {
    const group = el("fieldset", "browse-choices"); group.append(el("legend", "", title), el("p", "muted", hint));
    const list = el("div", "browse-choice-list");
    const known = new Set(options.map(item => item.value));
    for (const value of draft[key] || []) if (!known.has(value)) options.push({value, label: value});
    const inputs = options.map(({value, label}) => {
      const field = el("label", "browse-toggle"), input = el("input"); input.type = "checkbox";
      input.value = value; input.checked = (draft[key] || []).includes(value);
      field.append(input, el("span", "", label)); list.append(field); return input;
    });
    if (!options.length) list.append(el("p", "muted", "暂无可选项目"));
    group.append(list); content.append(group);
    readers.push(() => {draft[key] = inputs.filter(input => input.checked).map(input => input.value);});
  }
  choices("computer_nodes", "哪些地点有电脑", (world.nodes || []).map(node => ({value: node.id, label: node.name || node.id})), "不选地点，就不会安排看电脑消息。这里的设备属于角色世界，无需配置服务器电脑。");
  choices("sessions", "可以翻阅哪些会话", sessions.map(item => ({value: item.session_id, label: item.label || item.note || item.group_name || item.session_id})), "留空表示同一生活组内已启用的会话。勾选后只读所选范围，不跨生活组。私聊感想只在原私聊使用。");
  const grid = el("div", "browse-limits");
  for (const [key, label, min, max] of [["cooldown_minutes", "两次浏览至少间隔（分钟）", 30, 1440], ["daily_max", "每天最多浏览次数", 1, 12]]) {
    const field = el("label", "field"), input = el("input"); input.type = "number"; input.required = true;
    input.min = min; input.max = max; input.value = draft[key]; field.append(el("span", "", label), input); grid.append(field);
    readers.push(() => {if (!input.checkValidity()) throw new Error(`${label}超出范围`); draft[key] = Number(input.value);});
  }
  content.append(grid);
  toggle("remember_meaningful", "有持续意义的感受可留下生活记忆");
  content.append(el("p", "muted", "默认每次最多读取最近 6 小时内的 20 条文字、3,000 字，只用插件已收到的内容。普通感想保留 24 小时；有意义的感受仍保留来源，不成为群聊摘要。共享生活组共用次数和冷却，也服从后台预算。"));
  const advanced = el("details", "browse-advanced"); advanced.append(el("summary", "", "调整阅读窗口"));
  for (const [key, label, min, max] of [["window_hours", "消息范围（小时）", 1, 24], ["max_messages", "每次最多读取条数", 1, 40], ["max_chars", "每次正文最多字数", 300, 6000]]) {
    const field = el("label", "field"), input = el("input"); input.type = "number"; input.required = true;
    input.min = min; input.max = max; input.value = draft[key]; field.append(el("span", "", label), input); advanced.append(field);
    readers.push(() => {if (!input.checkValidity()) throw new Error(`${label}超出范围`); draft[key] = Number(input.value);});
  }
  content.append(advanced); panel.append(content); body.append(panel);
  return () => {readers.forEach(read => read()); return draft;};
}
