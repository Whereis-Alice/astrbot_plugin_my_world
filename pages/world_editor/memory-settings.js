/* One retrieval form for the settings page and the life-memory shortcut. */
export function memorySearchFields({container, memory, providers = [], defaultProvider = "", el, onChange = () => {}}) {
  const root = el("div", "memory-search-settings full");
  memory.semantic_source ||= memory.semantic_endpoint && memory.semantic_model ? "custom" : "provider";
  container.append(root);
  function field(label, input, hint = "") {
    input.setAttribute("aria-label", label);
    const wrap = el("label", "field"), head = el("span", "field-head");
    if (input.type === "checkbox") wrap.classList.add("memory-toggle");
    head.append(el("span", "", label)); wrap.append(head, input);
    if (hint) { const note = el("small", "muted", hint); note.dataset.tip = hint; wrap.append(note); }
    root.append(wrap); return input;
  }
  function check(key, label, hint) {
    const input = el("input"); input.type = "checkbox"; input.checked = Boolean(memory[key]);
    field(label, input, hint); input.addEventListener("change", () => { memory[key] = input.checked; onChange(); });
  }
  function select(key, label, choices, fallback) {
    const input = el("select");
    for (const [value, name] of choices) input.add(new Option(name, value));
    input.value = memory[key] || fallback;
    field(label, input);
    input.addEventListener("change", () => { memory[key] = input.value; onChange(); render(); });
  }
  function render() {
    root.replaceChildren();
    check("semantic_enabled", "启用语义召回", "用意思相近的表达查找自己的生活经历。可选；关闭或请求失败时继续使用关键词检索。");
    select("semantic_source", "嵌入模型来源", [["provider", "AstrBot 已配置的嵌入模型"], ["custom", "自定义兼容接口"]], "provider");
    if ((memory.semantic_source || "provider") === "provider") {
      const choices = [["", defaultProvider ? `跟随插件配置 · ${defaultProvider}` : "跟随插件配置（尚未指定）"], ...providers.map(p => [p.id, p.model ? `${p.id} · ${p.model}` : p.id])];
      if (memory.semantic_provider_id && !providers.some(p => p.id === memory.semantic_provider_id)) choices.push([memory.semantic_provider_id, `${memory.semantic_provider_id}（当前未加载）`]);
      select("semantic_provider_id", "生活记忆嵌入模型", choices, "");
      root.append(el("p", "muted", "复用 AstrBot 的连接与密钥。也可在本插件配置中指定默认模型；这里使用嵌入模型。"));
    } else {
      for (const [key, label, hint] of [
        ["semantic_endpoint", "嵌入服务 API 基址（含 /v1）", "填写兼容 embeddings 接口的服务地址。"],
        ["semantic_model", "嵌入模型名称", "填写该服务提供的嵌入模型。"],
        ["semantic_api_key_env", "保存 API Key 的服务器环境变量名", "这里只填环境变量名，密钥留在服务器；不要把密钥直接填进来。"],
      ]) {
        const input = el("input"); input.value = memory[key] || "";
        field(label, input, hint);
        input.addEventListener("input", () => { memory[key] = input.value.trim(); onChange(); });
      }
    }
    check("life_rerank", "主动查询生活记忆时重排", "仅在主动查询且有多个候选时，额外调用一次判断模型；默认关闭，不增加普通聊天的调用。");
  }
  render();
}
