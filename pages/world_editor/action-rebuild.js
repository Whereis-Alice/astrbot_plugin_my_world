/** Rebuild location activities without changing the map or out-of-scope bindings. */
export function actionRebuildPlan(config, nodeIds, drafts, grouping = "node") {
  const next = structuredClone(config), world = next.world;
  const targets = new Set(nodeIds), removed = [], shared = [], added = [];
  if (!targets.size || nodeIds.some(id => !world.nodes.some(n => n.id === id))) throw new Error("重做范围已变化，请重新生成");
  world.actions = world.actions.filter(action => {
    if (action.builtin || action.scope !== "node" || !(action.allowed_nodes || []).some(id => targets.has(id))) return true;
    const kept = action.allowed_nodes.filter(id => !targets.has(id));
    if (kept.length) { action.allowed_nodes = kept; shared.push(action.id); return true; }
    removed.push(action); return false;
  });
  // Never reuse removed identities: queued plans must not execute a different activity.
  const used = new Set(config.world.actions.map(a => a.id));
  const covered = new Set();
  for (const draft of drafts.filter(a => a._pick !== false)) {
    const action = structuredClone(draft); delete action._pick;
    if (action.scope !== "node" || !action.allowed_nodes?.length || action.allowed_nodes.some(id => !targets.has(id)))
      throw new Error("新动作必须只绑定本次重做范围内的地点");
    const base = action.id; let i = 2;
    if (!base) throw new Error("新动作缺少编号");
    while (used.has(action.id)) action.id = `${base}_${i++}`;
    used.add(action.id); action.allowed_nodes.forEach(id => covered.add(id));
    const node = world.nodes.find(n => n.id === action.allowed_nodes[0]);
    const zone = world.zones.find(z => z.id === node.zone_id);
    action.group = grouping === "zone" ? (zone?.name || zone?.id || "地点生活") : (node.name || node.id);
    action.builtin = false; action.created_at = Date.now();
    world.actions.push(action); added.push(action);
  }
  if ([...targets].some(id => !covered.has(id))) throw new Error("每个选定地点至少保留一个新动作，避免清空某个地点的活动");
  const removedIds = new Set(removed.map(a => a.id));
  const schedules = (next.schedules?.schedules || []).filter(s => (s.action_chain || []).some(step => removedIds.has(step.type)));
  // Keep the old chains for manual remapping; never execute a partly missing chain.
  schedules.forEach(s => { s.enabled = false; });
  const resetRecovery = removedIds.has(world.sleep?.recovery_action_id);
  if (resetRecovery) world.sleep.recovery_action_id = "";
  return {config: next, removed, shared, added, schedules, resetRecovery,
    removedGroups: [...new Set(removed.map(a => a.group).filter(Boolean))].filter(group => !world.actions.some(a => a.group === group))};
}

export function openActionRebuild(scope, api) {
  const {ui, el, nodes, askGenerateCount, apiPost, openCustomDialog, buildActionDraftList, markDirty, render, saveAll, toast} = api;
  const selected = nodes().filter(n => scope.node ? n.id === scope.node : scope.zone ? n.zone_id === scope.zone : true);
  if (!selected.length) { toast("这个范围没有地点"); return; }
  const label = scope.node ? selected[0].name : scope.zone ? "这个区域" : "整张地图";
  askGenerateCount({title: `保留地图，重做${label}的动作`, defaultValue: 3, min: 1, max: 5,
    hint: `为 ${selected.length} 个地点分别生成下面数量的动作；替换地点专属动作和分组，保留地图、通用动作和内置基础动作。跨范围共享动作保留外部绑定。生成失败不替换旧内容。`,
    extra: {placeholder: "例如：艾拉不睡觉，通过充电恢复精力；卧室安排充电，厨房安排烹饪。"},
    choice: {label: "重新分组", defaultValue: "node", options: [{value: "node", text: "每个地点一组"}, {value: "zone", text: "每个区域一组"}]},
    onRun: async (count, _second, extra, grouping, _third, report = () => {}) => {
      if (ui.dirty && !(await saveAll())) throw new Error("请先保存当前地图与人设");
      const config = ui.config, owner = ui.scope, baseline = JSON.stringify(config);
      const current = () => ui.config === config && ui.scope === owner && JSON.stringify(ui.config) === baseline;
      let pending = null, pendingSnapshot = "";
      const drafts = [], problems = [];
      for (const node of selected) {
        if (!current()) throw new Error("生成期间配置已变化，旧结果未应用");
        report(`正在生成第 ${selected.indexOf(node) + 1}/${selected.length} 个地点：${node.name || node.id}`);
        const result = await apiPost("generate/actions", {node: node.id, per_node: count, replacing: true,
          extra_prompt: `本次重新设计这个地点的完整活动，不需要回避原有活动名称。${extra || ""}`});
        if (!current()) throw new Error("生成期间配置已变化，旧结果未应用");
        const valid = (result.actions || []).filter(a => a.scope === "node" && a.allowed_nodes?.length && a.allowed_nodes.every(id => id === node.id));
        if (!valid.length) throw new Error(`${node.name || node.id} 未生成可用动作；本次未替换任何旧动作。`);
        drafts.push(...valid); problems.push(...(result.problems || []));
      }
      const plan = actionRebuildPlan(config, selected.map(n => n.id), drafts, grouping);
      return () => openCustomDialog({title: `预览重做动作 · ${selected.length} 个地点`, confirmText: "替换动作并保存",
        hint: `删除 ${plan.removed.length} 个旧动作，调整 ${plan.shared.length} 个共享绑定，加入 ${plan.added.length} 个新动作。地图保持原样。`,
        build: body => {
          body.append(el("p", "muted", `旧分组清理：${plan.removedGroups.join("、") || "没有空分组"}。新动作按${grouping === "zone" ? "区域" : "地点"}重新分组。`));
          if (plan.schedules.length) body.append(el("p", "hint", `这些日程引用旧动作，将停用并保留原步骤供重新选择：${plan.schedules.map(s => s.name || s.id).join("、")}`));
          if (plan.resetRecovery) body.append(el("p", "hint", "原首选恢复动作将删除，恢复设置改为自动选择；可在新动作中重新指定。"));
          const old = el("details"); old.append(el("summary", "", "将替换的旧动作"), el("p", "", plan.removed.map(a => a.name || a.id).join("、"))); body.append(old);
          buildActionDraftList(body, drafts, problems);
        },
        onSubmit: async () => {
          const retry = ui.scope === owner && ui.config === pending && JSON.stringify(pending) === pendingSnapshot;
          if (!current() && !retry) { toast("配置已变化，请重新生成，避免覆盖刚才的编辑"); return false; }
          try {
            const final = actionRebuildPlan(config, selected.map(n => n.id), drafts, grouping);
            ui.config = pending = final.config; pendingSnapshot = JSON.stringify(pending);
            markDirty(); render();
            const saved = await saveAll();
            if (!saved) toast("保存未完成，草稿仍保留；可再次点击替换动作并保存");
            return saved;
          } catch (error) { toast(error.message); return false; }
        },
      });
    },
  });
}
