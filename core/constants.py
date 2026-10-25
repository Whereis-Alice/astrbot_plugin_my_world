"""插件级常量：识别名、钩子优先级、事件附加字段与等待上限。
"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_my_world"
PLUGIN_VERSION = "v4.38.26"
"""插件版本：与 metadata.yaml 保持一致，`@register` 也读它，别再各写一份。"""
# 给主人格 / 别的插件用的日程工具（配置里可以关掉，见 `expose_schedule_tools`）
SCHEDULE_TOOL_NAMES = ("mw_schedule_list", "mw_schedule_add", "mw_schedule_remove")
# 状态页那些按钮（推进 tick / 触发决策 / 打断 / 叫醒）最多等这么久：
# 她正在检索或调模型时可能占着会话，超时就明确回一句，别让前端"点了没反应"
STATE_ACTION_TIMEOUT = 6.0
# 「推进 tick」这一下最多等多久（秒）：这一轮里可能夹着大模型调用（刚走到新地点要就地决定、
# 检索…），等不到就把它留在后台跑完，**绝不中途取消**——取消会把这一轮砍在半路。
STATE_TICK_WAIT_SECONDS = 45.0
# 钩子优先级：取一个很低的数字，让我们**排在所有插件后面**。
# AstrBot 按 priority 从高到低执行钩子，而且一旦某个钩子 stop 了事件，后面的钩子就不再执行；
# 本插件接管时会 stop_event()，所以必须抢在最后：记忆插件、上下文插件、世界书、环境感知……
# 这一轮往请求里写的东西都要先写完，我们再把它们一起带进她的提示词。
# （原来的值是 -100，任何注册在 -100 以下的插件都轮不到跑，它们的上下文就这么丢了。）
LLM_HOOK_PRIORITY = -1_000_000
# 睡觉门禁要抢在「意图路由」这类消息级插件前面：它们通常注册在 100 左右，
# 取值比它们高才会先执行；一旦这里 stop_event()，后面的处理器都不会跑。
SLEEP_GUARD_PRIORITY = 200
# 「旁观记录」也要抢在它们前面。意图路由会把消息**先吞下去再挑一部分重投**，
# 被吞掉的那几条如果排在它后面才记录，就永远记不到——她看到的聊天记录会缺一大段。
# 排在门禁之后（门禁要第一个拦睡眠）、路由之前。
PRESENCE_PRIORITY = 190
# 同一条会话的前一条回复最多让后一条等这么久；超时就不再排队（宁可多说一句，也别一直不说话）
REPLY_TURN_WAIT_SECONDS = 25.0
# 事件上挂的「这条消息已经在待回队列里了」标记（连发合并用）
MW_INCOMING_EXTRA = "mw_incoming_record"
# 平台不给 message_id 时，同一条消息的**稳定内部事件 id** 挂在这里：
# 旁观监听和 LLM 请求两个钩子拿到的是同一个 event 对象，因此复用同一个 id。
MW_EVENT_ID_EXTRA = "mw_event_id"
# 注入式接管：这一轮的状态挂在事件上，观察钩子（回复 / 工具 / 发送完成）共用它
MW_TURN_EXTRA = "mw_dialogue_turn"
# 回复模式：inject = 只注入提示词（默认，推荐）；takeover = 旧版自己调模型自己发（已废弃，仅作迁移兜底）
DEFAULT_REPLY_MODE = "inject"
TOKEN_TTL_SECONDS = 24 * 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
DATA_DIR_ENV = "MY_WORLD_DATA_DIR"
MIGRATE_FROM_ENV = "MY_WORLD_MIGRATE_FROM"
"""可选环境变量：指定从哪个目录搬数据过来（``none`` = 不搬）。"""
SOURCE_PLUGIN_NAMES = ("astrbot_plugin_her_world", "astrbot_plugin_virtual_world")
"""旧插件的识别名，按顺序找：本插件是它们的二次开发版，第一次启动时可以整份搬数据。"""
ORIGINAL_PLUGIN_NAME = SOURCE_PLUGIN_NAMES[-1]
"""上游原插件的识别名（日志与兼容用）。"""
