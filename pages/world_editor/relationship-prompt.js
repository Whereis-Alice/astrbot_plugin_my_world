/** Optional authored context, shared by relationship and distance editors. */
export function relationshipPromptEditor({item, kind, el, onChange = () => {}}) {
  const box = el("details", "relationship-prompt");
  const head = el("summary");
  const status = el("span", "");
  head.append(el("strong", "", "额外说明（可选）"), status);
  const hint = el("p", "muted", "默认只说明关系身份和相处距离，具体言行遵循原有人设。仅在需要时补充，不必填写性格或固定反应。");
  const label = el("label", "relationship-prompt-field");
  const text = el("textarea"); text.rows = 3; text.value = item.prompt || "";
  text.setAttribute("aria-label", `${kind}额外说明`);
  text.placeholder = "留空即可，按原有人设自然相处。";
  label.append(el("span", "", `${kind}额外说明`), text);
  const clear = el("button", "ghost small", "清空额外说明"); clear.type = "button";
  const sync = () => {
    status.textContent = String(item.prompt || "").trim() ? "已自定义" : "未添加 · 遵循人设";
    clear.disabled = !text.value;
  };
  text.addEventListener("input", () => {item.prompt = text.value; item.prompt_customized = true; sync(); onChange();});
  clear.addEventListener("click", () => {text.value = item.prompt = ""; item.prompt_customized = true; sync(); onChange();});
  box.open = Boolean(String(item.prompt || "").trim());
  box.append(head, hint, label, clear); sync(); return box;
}
