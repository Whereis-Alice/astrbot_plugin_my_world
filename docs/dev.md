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

### 重构与维护边界

`core/timeline.py` 保留日志和调试输出的公共入口；正文由 `core/timeline_renderers/` 按消息与表达、动作与日程、工具与平台、生活节奏、记忆、世界事件分派，格式化工具在 `common.py`。新增日志类型只放进对应职责并登记分派，不把全部类型重新塞回入口。正常文案、精简模式、扩展优先级和未知类型摘要保持兼容；异常历史字段只影响本条显示，不改写数据库或推断角色行为。`tests/test_timeline_resilience.py` 验证正式日志列表、导出、原始记录保留与会话范围，`tests/test_structure.py` 防止渲染再次集中为长函数。

恢复配置的目标和门槛公式集中在 `core/recovery.py` 的 `recovery_target`、`recovery_needed`。两个函数只读取输入：是否采用活动恢复仍由调用方的模式决定，恢复意图的写入和清除仍由 `core/engine/recovery.py` 负责，危急事件、日程与直接聊天的优先级在原入口处理。规则候选的初始低精力判断和生活选择的持续恢复意图分别显式传入，避免重构时无意扩大门禁。改动恢复行为时，先跑 `tests/test_recovery.py` 和 `tests/test_life_review_round11_20261024.py` 中的实际入口回归。

大型模块分次拆分，以可验证的职责为单位；保留已使用的公共导入，先对照旧实现，再跑调用方回归。不要仅以减少行数为目标，增加新的 mixin、全局状态或相互引用。

## 本地验证与测试

```powershell
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m ruff check .
python -m pytest -q
```

Linux CI 使用 `python -m playwright install --with-deps chromium` 安装浏览器和系统库。未安装 Playwright 或 Chromium 时，浏览器回归会跳过；完整验收应安装二者。生产环境仍只需要 `requirements.txt` 中的插件依赖。

pytest 的会话夹具统一使用 Asia/Shanghai，与默认测试世界一致，使旧用例里的无时区 `datetime.timestamp()` 与测试时钟处于同一天；测试结束会恢复原环境。其他世界时区的边界用例使用明确时区，不依赖机器所在地。

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

生活工作区由 `pages/world_editor/life-workspace.js` 组织今日、牵挂与经历，复用原有日程和事件编辑器。`core/engine/life_control.py` 负责共同取舍、中断续作及事件关联，`life_inference.py` 负责生活决策的锁外等待与结果核对；聊天 Agent 不进入这条等待路径。

验证统一入口、跨模块接续、并发状态保护与六种主题布局：

```powershell
python -m pytest tests/test_life_unification.py tests/test_life_api.py -q
python -m pytest tests/test_life_workspace_browser.py tests/test_visual_workspace_browser.py -q
```

浏览器验收需要 Playwright 与 Chromium，使用临时数据库和合成数据，覆盖桌面、手机、管理抽屉、配置范围、生成、保存与关联跳转。

界面的主题与材质集中在 `observatory.css`，六套本机外观由 `appearance.js` 管理，工作区的次要操作菜单由 `workspace-ui.js` 初始化。`data-theme` 保留深浅语义，`data-palette` 指定配色；不要在组件里为每个主题复制颜色。可用 `python -m pytest tests/test_visual_workspace_browser.py -q` 验证六套主题、键盘关闭与恢复、文字对比度，以及各模块在四种屏幕宽度下的布局；合成截图输出到 `.pytest_cache/generation-ui/`。

控制台的矢量世界由 `world-scene.js` 提供，主视窗与六张主题缩略图使用各自的 SVG ID，避免渐变引用串色。视窗文字使用状态快照，装饰插画不推断世界行为；减少动态效果会停用悬浮、指针景深与切页过渡。运行 `python -m pytest tests/test_observatory_browser.py -q` 可生成六主题、390/360px 手机截图，并检查实时状态、曲线无障碍说明、键盘展开与只读操作。截图全部使用合成数据。

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

生活事项只推进世界内活动与 AI 模拟，测试见 `tests/test_life_simulation.py`，覆盖计时、暂停、旧存档清理及现实约定的完成边界。

共存测试可设置 `MY_WORLD_MOTION_VISION_SOURCE`、`MY_WORLD_ANAMNESIS_SOURCE` 为对应源码目录，复用实际媒体注入与聊天记忆注入代码。浏览器测试使用合成数据，不连接真实会话。

## 推送到 GitHub

如果遇到只能访问 `api.github.com`、`git push` 被重置的网络，
用 `tools/push_via_api.ps1` 通过 REST API 推送：

```powershell
pwsh tools/push_via_api.ps1 -Repo Whereis-Alice/astrbot_plugin_my_world
```

发布前先在本地完成完整测试、Ruff和必要的固定源码联测。脚本按 blob → tree → commit 的顺序上传当前HEAD，认证走本机已登录的 `gh`；先创建 `codex/ci-*` 验证分支，等待该分支、同一个提交的 `ci.yml` 完整成功，再更新main。测试失败、取消、等待超时或main在验证期间被别人更新，均停止晋级，保留验证分支便于查看；不删除失败记录或关闭通知。

等待CI默认最多30分钟，可用 `-CiTimeoutMinutes` 在1–60分钟内调整。脚本校验本地提交包含远端历史，并使用非强制更新；验证成功后直接晋级已经通过的API提交，不重新建一个未经验证的提交。main更新仍触发现有完整CI，保留仓库原有检查规则。

脚本用于已有main的仓库；读取main失败时直接停止，不把网络或权限错误当成空仓库，也不通过Contents API在验证前初始化main。
