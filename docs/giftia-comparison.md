# 从 Giftia 学习什么

[文档目录](README.md) · [生活设计审视](life-design-review.md)

本次对照的是 [Giftia 固定源码快照](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/tree/2390886d6c377d77d2236b90fe8969ef36fa86e3)。群聊里的自然反应和无人聊天时持续生活，需要分别评估。

## 值得继续学习

| 借鉴点 | Giftia 的实现 | 本插件现状与适合的做法 |
| --- | --- | --- |
| 具体相处方式 | 根据未评估的关系变化历史归纳简短互动态度，冻结处理范围，保存游标后推进。 | 已有身份、好感、档位和证据，仍可补充有来源的相处习惯；不能通过总结绕过关系权限。 |
| 更准确的召回 | 作用域内语义搜索后可再重排，并记录命中。 | 已有可选嵌入召回和关键词兜底。优先增加召回依据、排查与可选重排，无需再接一套记忆库。 |
| 活跃人物上下文 | 总结只取本批出现的人物，称呼只影响渲染，不改来源原话。 | 已有不可变来源和上下文资格；可继续收紧人物预算，避免无关档案挤占本轮。 |
| 失败后的回复边界 | 固定消息批次，失败留状态，避免重放整轮而重复工具或部分发送；派发动作时标识副作用开始。 | 默认聊天由宿主执行，不应另造队列重跑原生工具。可增强工具和发送记录关联，但尚无完整原生副作用账本。 |
| 按需理解媒体 | 媒体有持久 ID 和转述，可显式检查图片、音频、视频；视频片段不同则不随便复用。 | 图片缓存和并发去重已经存在。本次修复等待者挂起和 URL 内容混淆；未来可让媒体先留索引，被问到再深入看。 |
| 原子容量约束 | 单条 SQL 同时检查任务容量和插入，避免并发超过上限。 | 事项在角色状态锁内变更，无需照搬 SQL；本次补齐暂停后恢复、延期的活跃容量检查。将来拆表也需保持原子性。 |

源码：[互动态度维护](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/memory/passive_tasks.py)、[人物上下文](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/memory/passive_context.py)、[召回与重排](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/memory_recall.py)、[回复批次](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/chat_manager.py)、[媒体检查](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/conversation/media_captioner.py)、[任务容量](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia/blob/2390886d6c377d77d2236b90fe8969ef36fa86e3/core/database/repositories/short_tasks.py)。

## 应继续加强的自身方向

本插件有空间、耗时动作、独立睡眠、日程、事件、需求和跨天事项，共同承担没有人说话时日子也继续的目标。Giftia 的短期任务板把任务带入决策与回复上下文；这本身不能替代地图中的执行步骤、在线投入结算和跨阶段接续。

应加强这些系统之间的取舍，以及经历对后续偏好的影响。不要为了更多聊天反应，把独立生活重新绑定到新消息；无需照搬 XML 回复解析、输出清洗或另建工具循环。主回复继续使用 AstrBot 原生 Agent，生活引擎提供连贯、适量的处境。

以上是源码对照，没有做相同模型、消息量、硬件下的长期对照运行，不能据此断言谁总体更聪明、更省 Token 或更稳定。语义归纳、身份确认和结果解释仍受模型质量影响，需要保留来源、人工修正和保守失败路径。
