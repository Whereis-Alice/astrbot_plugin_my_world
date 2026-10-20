/** Optional boundaries: select enabled actions by name, retain existing unavailable selections. */
export function relationshipBoundaryEditor({item, actions = [], el, onChange = () => {}}) {
  const box = el("details", "relationship-boundary");
  const head = el("summary");
  const status = el("span");
  head.append(el("strong", "", "这一档还不能做（可选）"), status);
  const hint = el("p", "muted", "默认留空，按人设和情境自然相处。需要额外限制时，从已启用的动作中选择；此处仅限制当前档位。");
  const selected = el("div", "boundary-selected");
  const search = el("input"); search.type = "search";
  search.placeholder = "搜索已启用的动作名称";
  search.setAttribute("aria-label", "搜索已启用的动作");
  const choices = el("div", "boundary-options");
  const catalog = new Map(actions.map(action => [String(action.id), action]));
  const enabled = actions.filter(action => action.enabled !== false);
  const update = ids => {item.deny = [...ids]; item.deny_customized = true; render(); onChange();};
  function render() {
    const ids = new Set(item.deny || []);
    status.textContent = ids.size ? `已限制 ${ids.size} 项` : "未添加 · 遵循人设";
    selected.replaceChildren(); choices.replaceChildren();
    for (const id of ids) {
      const action = catalog.get(id);
      const chip = el("button", "boundary-chip"); chip.type = "button";
      const name = action?.name || id;
      const suffix = !action ? " · 动作不可用" : action.enabled === false ? " · 已停用" : "";
      chip.append(el("span", "", name + suffix), el("span", "", "×"));
      chip.setAttribute("aria-label", `移除限制：${name}`);
      chip.addEventListener("click", () => {ids.delete(id); update(ids);});
      selected.append(chip);
    }
    selected.hidden = !ids.size;
    const query = search.value.trim().toLocaleLowerCase();
    const matches = enabled.filter(action => `${action.name || ""} ${action.id}`.toLocaleLowerCase().includes(query));
    for (const action of matches) {
      const label = el("label", "boundary-option");
      const check = el("input"); check.type = "checkbox"; check.checked = ids.has(action.id);
      check.addEventListener("change", () => {check.checked ? ids.add(action.id) : ids.delete(action.id); update(ids);});
      label.append(check, el("span", "", action.name || action.id)); choices.append(label);
    }
    if (!matches.length) choices.append(el("p", "muted", enabled.length ? "没有匹配的动作" : "还没有已启用的动作，可先到动作页启用。"));
  }
  search.addEventListener("input", render);
  box.open = Boolean(item.deny?.length);
  box.append(head, hint, selected, search, choices); render(); return box;
}
