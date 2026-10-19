# 开发与测试

[文档目录](README.md) · [按主题拆开的详细说明](GUIDE.md) · [返回 README](../README.md)

## 目录结构

```text
astrbot_plugin_my_world/
├── main.py                  # 入口与编排：钩子、指令、生命周期、接线
├── metadata.yaml            # 插件标识与版本（插件市场读它）
├── _conf_schema.json        # AstrBot 面板里的插件配置项
├── core/                    # 领域层（不依赖 AstrBot）：世界模型、提示词、状态、记忆、事件
│   └── engine/              # 引擎本体：MyWorldEngine = 一组按职责拆分的 mixin（配置 / 事件 / 回复 / 行动…）
├── adapters/                # 宿主绑定：模型调用、看图、指令、发送、工具、人设
├── web/                     # 编辑器接口：WebApiMixin 与访问密码
├── pages/world_editor/      # 网页编辑器（HTML / CSS / 原生 JS）
├── docs/                    # 文档（本文件在这里）
├── tests/                   # 不依赖 AstrBot 的回归测试（自带桩）
└── tools/                   # 开发脚本：logo、模拟、API 推送
```

核心逻辑都在 `core/`；`core/engine/` 里每个模块只放一块职责（`reply.py` 管接管回复、`weather.py` 管天气、
`actions.py` 管动作执行……），`MyWorldEngine` 自己只负责 `__init__` 和把它们拼起来。
`adapters/` 只做「宿主能力 → `core/ports.py` 协议」的翻译，`web/` 只做编辑器 HTTP 接口，
`main.py` 只做编排。测试替身在 `tests/fake_astrbot.py` 与
`tests/stub_ports.py` 里，所以跑测试不需要装 AstrBot、也不碰真实数据目录。

## 本地验证与测试

```powershell
python -m pytest -q
```

覆盖范围：提示词分层与注入边界、回合复盘、世界生成解析、引擎状态机、事件与掷骰、
记忆与画像、编辑器接口与前端引用、旧版接管路径（迁移兜底）等。

想只跑某一块：

```powershell
python -m pytest tests/test_dialogue_inject.py -q   # 注入式接管（主路径）
python -m pytest tests/test_world_generator.py -q   # 区域 / 日程 / 事件生成
python -m pytest tests/test_frontend.py -q          # 编辑器契约
python -m pytest tests/test_contacts.py -q          # 合并画像、工具读取、静默生活和排程恢复
python -m pytest tests/test_contacts_browser.py -q  # 需要 Playwright + Chromium
```

通讯录的加载和切换状态在 `pages/world_editor/contact-workbench.js`，布局在 `contacts.css`。
它通过依赖注入复用编辑器的 API 与字段编辑入口，不依赖打包器。浏览器回归使用合成资料，覆盖长详情、66 人列表、短视口、手机布局、异步请求竞态与侧栏图标。

界面的主题与材质集中在 `atelier.css`，六套本机外观由 `appearance.js` 管理，工作区的次要操作菜单由 `workspace-ui.js` 初始化。`data-theme` 保留深浅语义，`data-palette` 指定配色；不要在组件里为每个主题复制颜色。可用 `python -m pytest tests/test_visual_workspace_browser.py -q` 验证六套主题、键盘关闭与恢复、文字对比度，以及十五个模块在四种屏幕宽度下的布局；合成截图输出到 `.pytest_cache/generation-ui/`。

单选框通过 `@supports` 渐进使用 `appearance: base-select` 和 `::picker(select)`，保留原生的值、键盘操作和 `change` 事件；不支持时使用原生菜单及明确的选项配色。多选和带 `size` 的列表不启用弹出面板样式。

## 两个版本开关

- `reply_mode=inject`（默认）：只注入；`takeover` 是旧版接管（自己调模型自己发），
  仅作迁移兜底。
- `legacy_tool_actions`（默认关）：旧版工具 / 指令动作是否继续执行；
  也可以用环境变量 `MY_WORLD_LEGACY_TOOL_ACTIONS=1`。

## 数据目录

默认在 AstrBot 的 `data/plugin_data/astrbot_plugin_my_world/`，可以用环境变量
`MY_WORLD_DATA_DIR` 覆盖。第一次启动时可以用 `MY_WORLD_MIGRATE_FROM=<旧目录>`
从 `astrbot_plugin_her_world` / `astrbot_plugin_virtual_world` 整份搬数据（不覆盖已有数据）。

## 原生 Agent 与插件共存验证

生活执行的离线 SDK 契约复现脚本为 `tools/verify_life_sdk.py`。将仓库与 AstrBot SDK 放到 `PYTHONPATH`，从一次性临时目录运行，避免 SDK 的默认配置初始化写入开发目录。脚本运行真实原生 Runner 与 Shell 工具，仅模型和沙盒传输使用合成桩，不连接服务器或执行宿主命令；这不能替代实际沙盒服务的隔离测试。

共存测试可设置 `MY_WORLD_MOTION_VISION_SOURCE`、`MY_WORLD_ANAMNESIS_SOURCE` 为对应源码目录，复用实际媒体注入与聊天记忆注入代码。浏览器测试使用合成数据；执行账本的失败、暂停、重启与防重场景见 `tests/test_life_execution.py`。

## 推送到 GitHub

如果遇到只能访问 `api.github.com`、`git push` 被重置的网络，
用 `tools/push_via_api.ps1` 通过 REST API 推送：

```powershell
pwsh tools/push_via_api.ps1 -Repo Whereis-Alice/astrbot_plugin_my_world
```

它按 blob → tree → commit → ref 的顺序推当前 HEAD 的内容，认证走本机已登录的 `gh`。
推送前校验本地提交包含远端历史；更新分支时使用非强制方式，远端并发更新会被拒绝。
