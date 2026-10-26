import { relationshipBoundaryEditor } from "./relationship-boundary.js";
import { applyDetailedLevels, friendshipPresetControls } from "./relationship-preset.js";
import { familiarityDescriptionEditor, relationshipPromptEditor } from "./relationship-prompt.js";

const TEMPLATES = [
  ["同学", "classmate", 0, 30, "日常身份"], ["同事", "colleague", 0, 30, "日常身份"],
  ["室友", "roommate", 10, 55, "日常身份"], ["邻居", "neighbor", 0, 30, "日常身份"],
  ["搭档", "partner", 15, 60, "共同经历"], ["旧友", "old_friend", 15, 60, "共同经历"],
  ["青梅竹马", "childhood_friend", 30, 80, "共同经历"],
  ["师父", "teacher", 10, 60, "约定身份"], ["徒弟", "student", 10, 60, "约定身份"],
  ["侍从", "attendant", 10, 60, "约定身份"], ["伴侣", "romance", 55, 100, "约定身份"],
];

export async function openRelationshipRulesEditor({ el, apiGet, apiPost,
  openCustomDialog, toast, onSaved }) {
  let loaded;
  try { loaded = await apiGet("relationships/rules", {config_scope: ""}); }
  catch (error) { toast(error.message || "规则读取失败"); return; }
  const profile = structuredClone(loaded.profile);
  let tab = "bonds";
  const button = (text, fn, cls = "ghost small") => {
    const node = el("button", cls, text); node.type = "button"; node.addEventListener("click", fn); return node;
  };
  const input = (value, save, type = "text") => {
    const node = el("input"); node.type = type; node.value = String(value ?? "");
    node.addEventListener("input", () => save(type === "number" ? Number(node.value) : node.value)); return node;
  };
  const field = (label, node, hint = "") => {
    const wrap = el("label", "rule-field"); wrap.append(el("span", "", label), node);
    if (hint) wrap.append(el("small", "people-muted", hint)); return wrap;
  };
  const check = (label, value, save) => {
    const node = el("input"); node.type = "checkbox"; node.checked = Boolean(value);
    node.addEventListener("change", () => save(node.checked));
    const wrap = el("label", "rule-check"); wrap.append(node, el("span", "", label)); return wrap;
  };
  const select = (value, choices, save) => {
    const node = el("select"); choices.forEach(([id, name]) => { const opt = el("option", "", name); opt.value = String(id); node.append(opt); });
    node.value = String(value); node.addEventListener("change", () => save(node.value)); return node;
  };
  const levelAt = value => {
    const levels = profile.levels || [];
    let index = 0;
    levels.forEach((level, i) => {if (value >= Number(level.min_affinity)) index = i;});
    return index;
  };
  openCustomDialog({title: "关系规则", hint: "这里设置全局默认。各会话若启用了独立设置，仍使用自己的规则。修改先留在草稿，保存后生效。",
    confirmText: "保存关系规则",
    build: body => {
      const container = el("div", "relationship-rules-body");
      body.append(container);
      const tabs = el("div", "people-filters rule-tabs");
      const content = el("div", "relationship-rules-content");
      for (const [key, title] of [["bonds", "关系类型"], ["levels", "相处档位"], ["growth", "普通友谊"]]) {
        const node = button(title, () => {tab = key; render();}); node.dataset.rulesTab = key; tabs.append(node);
      }
      container.append(tabs, content);
      const redraw = () => render();
      function bondCard(bond, index) {
        const card = el("details", "rule-card");
        const head = el("summary");
        head.append(el("strong", "", bond.name || "未命名关系"),
          el("span", "people-muted", `${bond.auto_form ? "允许自然形成" : "仅手动确立"} · ${bond.unique ? "唯一身份" : "可多人拥有"}`));
        const fields = el("div", "rule-card-fields");
        fields.append(field("关系名称", input(bond.name, value => {bond.name = value; head.querySelector("strong").textContent = value || "未命名关系";})),
          field("槽位", input(bond.slot, value => {bond.slot = value;}), "同槽位表示同类身份；启用唯一后，该会话只能由一人占用。"),
          field("替代组", input(bond.group || "", value => {bond.group = value;}), "同一个人在同组内只保留一种关系。留空可与其他身份并存。"),
          field("别名", input((bond.aliases || []).join("、"), value => {bond.aliases = value.split(/[、,，]/).map(x => x.trim()).filter(Boolean);})),
          field("最低相处档位", select(bond.floor || 0, profile.levels.map((x, i) => [i, x.name]), value => {bond.floor = Number(value);})),
          field("通常最高档位", select(bond.cap || 0, profile.levels.map((x, i) => [i, x.name]), value => {bond.cap = Number(value);})));
        const toggles = el("div", "rule-toggles");
        for (const [key, label] of [["auto_form", "允许自然形成"], ["allow_boundary_crossing", "允许越过通常上限"], ["unique", "唯一身份"], ["negative", "负面关系"]])
          toggles.append(check(label, bond[key], value => {bond[key] = value;}));
        const remove = button("删除此关系类型", () => {profile.bonds.splice(index, 1); redraw();}, "ghost small danger");
        card.append(head, fields, toggles, relationshipPromptEditor({item: bond, kind: "关系", el}), remove); return card;
      }
      function levels() {
        content.append(el("p", "people-muted", "档位只描述相处距离，关系只描述身份；具体言行由原有人设和当前情境决定。好感仍按身份上下限匹配档位，需要时可添加额外说明。"));
        content.append(check("补充熟悉程度描述", profile.familiarity_descriptions_enabled,
          value => {profile.familiarity_descriptions_enabled = value;}),
          el("p", "people-muted", "默认关闭，沿用原有关系上下文；开启后仅补充当前相处档位的短描述。已有自定义说明与动作边界按原规则使用。"));
        const preset = el("details", "rule-template-library");
        preset.append(el("summary", "", "使用十档相处方案"),
          el("p", "people-muted", "戒备 → 疏远 → 礼貌 → 初识 → 熟悉 → 投缘 → 信任 → 亲密 → 知己 → 相依。应用会换算身份上下限，保存前可取消。"),
          button("应用十档到当前草稿", () => {applyDetailedLevels(profile); redraw();}));
        content.append(preset);
        profile.levels.forEach((level, index) => {
          const card = el("details", "rule-card");
          const head = el("summary"); head.append(el("strong", "", level.name || "未命名档位"),
            el("span", "people-muted", `${level.min_affinity} 至 ${level.max_affinity}`));
          const fields = el("div", "rule-card-fields");
          fields.append(field("档位名称", input(level.name, value => {level.name = value; head.querySelector("strong").textContent = value;})),
            field("好感起点（包含）", input(level.min_affinity, value => {level.min_affinity = value;}, "number")),
            field("好感终点", input(level.max_affinity, value => {level.max_affinity = value;}, "number")));
          const denied = relationshipBoundaryEditor({item: level, actions: loaded.actions || [], el});
          card.append(head, fields, familiarityDescriptionEditor({item: level, el}),
            relationshipPromptEditor({item: level, kind: "档位", el}), denied, button("删除此档位", () => {
            if (profile.levels.length <= 1) {toast("至少保留一个档位"); return;}
            profile.levels.splice(index, 1);
            for (const bond of profile.bonds) for (const key of ["floor", "cap"])
              bond[key] = Math.max(0, Number(bond[key] || 0) >= index ? Number(bond[key] || 0) - 1 : Number(bond[key] || 0));
            redraw();
          }, "ghost small danger"));
          content.append(card);
        });
        content.append(button("＋ 添加一个相处档位", () => {
          const end = Number(profile.levels.at(-1)?.max_affinity || 0);
          profile.levels.push({name: "新档位", min_affinity: end, max_affinity: end + 10, deny: [], deny_customized: false, prompt: ""}); redraw();
        }));
      }
      function growth() {
        content.appendChild(friendshipPresetControls({profile, preset: loaded.friendship_preset, el,
          onApplied: () => {redraw(); toast("已应用五阶段普通友谊，请保存关系规则");}, onError: message => toast(message)}));
        content.append(el("p", "people-muted", "只在当前会话积累直接交谈日期。普通身份按阶段缓慢发展，不会自动升成恋人，也不覆盖管理员设置。"),
          check("自动发展普通友谊", profile.relationship_auto_enabled, value => {profile.relationship_auto_enabled = value;}));
        (profile.friendship_stages || []).forEach((stage, index) => {
          const row = el("div", "rule-growth-stage");
          row.append(field("目标关系", select(stage.bond, profile.bonds.filter(bond => bond.group && !bond.unique && !bond.negative).map(bond => [bond.name, bond.name]), value => {stage.bond = value;})),
            field("不同日期直接交谈", input(stage.min_talk_days, value => {stage.min_talk_days = value;}, "number")),
            field("至少好感", input(stage.min_affinity, value => {stage.min_affinity = value;}, "number")),
            button("删除阶段", () => {profile.friendship_stages.splice(index, 1); redraw();}, "ghost small danger"));
          content.append(row);
        });
        content.append(button("＋ 添加普通阶段", () => {
          const bond = profile.bonds.find(item => item.auto_form && item.group && !item.unique && !item.negative);
          if (!bond) {toast("先为一种普通关系开启自然形成，并设置替代组"); return;}
          profile.friendship_stages = [...(profile.friendship_stages || []), {bond: bond.name, min_talk_days: 7, min_affinity: 20}]; redraw();
        }));
      }
      function render() {
        content.replaceChildren();
        tabs.querySelectorAll("button").forEach(node => node.classList.toggle("on", node.dataset.rulesTab === tab));
        if (tab === "levels") {levels(); return;}
        if (tab === "growth") {growth(); return;}
        content.append(el("p", "people-muted", "同学、室友、搭档等身份可以与朋友或恋人并存。新建身份默认只允许管理员设置；自然形成和越过上限需分别开启。"),
          field("初次认识的默认关系", select(profile.default_bond, profile.bonds.map(bond => [bond.name, bond.name]), value => {profile.default_bond = value;})));
        const library = el("details", "rule-template-library");
        library.append(el("summary", "", "从身份模板添加"));
        const templates = el("div", "rule-template-options");
        TEMPLATES.forEach(([name, slot, floor, cap, category]) => {
          const exists = profile.bonds.some(item => item.name === name);
          const node = button(`${name}${exists ? " ✓" : " ＋"}`, () => {
            profile.bonds.push({name, slot, group: slot === "romance" ? "close" : "", floor: levelAt(floor), cap: levelAt(cap),
              aliases: [], unique: slot === "romance", auto_form: false, allow_boundary_crossing: false, negative: false}); redraw();
          }); node.disabled = exists; node.title = category; templates.append(node);
        });
        library.append(templates); content.append(library);
        profile.bonds.forEach((bond, index) => content.append(bondCard(bond, index)));
        content.append(button("＋ 自定义一种关系", () => {profile.bonds.push({name: "新关系", slot: "", group: "", floor: 0,
          cap: profile.levels.length - 1, aliases: [], auto_form: false, allow_boundary_crossing: false, unique: false, negative: false}); redraw();}));
      }
      render();
    },
    onSubmit: async () => {
      const result = await apiPost("relationships/rules", {config_scope: "", profile, fingerprint: loaded.fingerprint});
      toast(result.warnings?.length ? result.warnings.join("；") : "关系规则已保存");
      await onSaved?.();
    },
  });
}
