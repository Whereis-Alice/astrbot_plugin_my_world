# 来源与署名

## 上游项目

本仓库是下面这个项目的二次开发版（fork / derivative work）：

- 项目：`astrbot_plugin_virtual_world`（虚拟世界 VW）
- 作者：**exneverbur**
- 仓库：https://github.com/exneverbur/astrbot_plugin_virtual_world
- 本仓库的基线版本：上游 **v2.1.8**

世界地图、动作系统、日程、数值与情绪、记忆与通讯录、事件机制、网页编辑器这些核心设计
都来自原作者。

## 中间版本

在改名成 **我的世界 MW** 之前，同一个仓库有一段时间叫 `astrbot_plugin_her_world`（她的世界 HW），
在 v2.2.x 里做过换名隔离、动作库管理与若干插件联动。v3.0.0 起改名为
`astrbot_plugin_my_world`（我的世界 MW），并重做了和 AstrBot 原生 Agent 的关系：

1. 主回复不再由本插件调模型、执行工具、自己发送——改成只注入人设与处境，工具循环与发送交回宿主；
2. 动作库重做成「虚拟动作 / 提示词事件」，不再绑定工具或指令；旧版工具动作只读保留并默认停用；
3. 新增区域 / 日程 / 事件的世界生成（会先读人设、已有世界与关系）；
4. 新增回合复盘，把她的状态与心思记进日志与状态；
5. 插件名、指令 `/mw`、数据目录、日志前缀与前端缓存键全部隔离。

旧数据（`astrbot_plugin_her_world` 或 `astrbot_plugin_virtual_world` 的数据目录）可以在第一次启动时整份搬过来：
设 `MY_WORLD_MIGRATE_FROM=<旧数据目录>`，或者让插件自动发现同机上的旧目录。

## 联动插件

下列插件由 [Whereis-Alice](https://github.com/Whereis-Alice) 维护；本插件只做**兼容适配**，
不会复制、修改或接管它们的内部逻辑：

- https://github.com/Whereis-Alice/astrbot_plugin_motion_vision
- https://github.com/Whereis-Alice/astrbot_plugin_outputpro_split
- https://github.com/Whereis-Alice/astrbot_plugin_dynamic_card_plus
- https://github.com/Whereis-Alice/astrbot_plugin_helper_tools
- https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie
- https://github.com/Whereis-Alice/astrbot_plugin_worldtree_lore
- https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm

## 许可说明

上游仓库当前**没有附带开源许可证文件**。因此本仓库同样不额外声明许可证：
它按上游同样的条件提供，一切权利与授权问题请以原作者为准。
如果你打算商用或再分发，请先与原作者（exneverbur）确认授权。

发现署名或来源有遗漏、或者你不希望自己的作品以这种方式被引用，请提 issue，我们会立刻处理。
