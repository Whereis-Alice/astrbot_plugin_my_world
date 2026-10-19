# 从 Giftia 学习什么

[文档目录](README.md) · [生活设计审视](life-design-review.md)

本次对照的是 [Giftia 固定源码快照](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/tree/2390886d6c377d77d2236b90fe8969ef36fa86e3)。群聊里的自然反应和无人聊天时持续生活，需要分别评估。

## 值得继续学习

| 借鉴点 | Giftia 的实现 | 本插件现状与适合的做法 |
| --- | --- | --- |
| 具体相处方式 | 根据未评估的关系变化历史归纳简短互动态度，冻结处理范围，保存游标后推进。 | 逐轮评价不读已有好感与长期结论；另行归纳最多三条有来源的习惯。游标、失败退避与人工覆盖防止反复推理或覆盖管理员设置，原有人设和权限优先。 |
| 更准确的召回 | 作用域内语义搜索后可再重排，并记录命中。 | 生活查询保留关键词与可选嵌入召回，可额外对最多十二条候选重排，默认关闭。返回编号、日期、来源与命中依据，并重新检查人工修改；不再接一套聊天记忆库。 |
| 活跃人物上下文 | 总结只取本批出现的人物，称呼只影响渲染，不改来源原话。 | 从可见发言按人检索、去重并共享预算；新原话提取与旧记忆整理分阶段。会话词典只学习明确解释、只在命中时注入。 |
| 失败后的回复边界 | 固定消息批次，失败留状态，避免重放整轮而重复工具或部分发送；派发动作时标识副作用开始。 | 聊天继续交给宿主，没有后续观测只记未知。独立生活执行保存逐轮工具开始、返回和产物收据；部分执行或未知结果先核对，不自动重放。聊天原生 Agent 的完整中间轮次仍不完全可见。 |
| 按需理解媒体 | 媒体有持久 ID 和转述，可显式检查图片、音频、视频；视频片段不同则不随便复用。 | 动态媒体、音轨与回看交给 Motion Vision，保留其临时内容和工具；已确认的媒体来源避免重复转述，未处理图片仍保留兜底。My World 不另建媒体索引或延长文件寿命。 |
| 原子容量约束 | 单条 SQL 同时检查任务容量和插入，避免并发超过上限。 | 事项在角色状态锁内变更，暂停后恢复或延期也重新检查活跃容量。将来拆表仍需保持原子性。 |

源码：[互动态度维护](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/memory/passive_tasks.py)、[人物上下文](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/memory/passive_context.py)、[召回与重排](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/memory_recall.py)、[回复批次](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/chat_manager.py)、[媒体检查](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/media_captioner.py)、[任务容量](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/database/repositories/short_tasks.py)。

## 应继续加强的自身方向

本插件有空间、耗时动作、独立睡眠、日程、事件、需求和跨天事项，共同承担没有人说话时日子也继续的目标。Giftia 的短期任务板把任务带入决策与回复上下文；这本身不能替代地图中的执行步骤、在线投入结算和跨阶段接续。

项目和自主安排已共用新工作准入，考虑困意、重要事件、续作与临近日程；约定在可执行事项中优先。活动收据可形成跨日时段参考，原有人设仍是言行主依据，不能把活动投入误写为新的喜恶。各日程和事件执行器仍保留，后续可继续统一更细的中断、放弃与恢复规则。

个人生活由世界内动作和 AI 模拟活动持续推进。编程、创作等个人项目可以分成跨天小步，按在线投入记录模拟经历，无需额外电脑环境。模拟成果不会被当成真实文件、真实测试或向用户发送成功；聊天中的实际工具调用继续由 AstrBot 原生 Agent 完成。

无需照搬 XML 回复解析、输出清洗或另建工具循环。主回复继续使用 AstrBot 原生 Agent，生活引擎提供连贯、适量的处境。学习和人工入口见 [相处习惯与共同用语](social-learning.md)，与现有记忆插件配合见 [Anamnesis 共存](memory-compatibility.md)。

以上是源码对照，没有做相同模型、消息量、硬件下的长期对照运行，不能据此断言谁总体更聪明、更省 Token 或更稳定。语义归纳、身份确认和结果解释仍受模型质量影响，需要保留来源、人工修正和保守失败路径。
