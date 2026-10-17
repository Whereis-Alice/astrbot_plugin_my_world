"""引擎级常量：节拍、检索预算、事件/回复阈值与口语时间正则。"""

from __future__ import annotations

# --- imports (filled by refactor) ---


DEFAULT_TICK_SECONDS = 60.0

EXTENSION_LAYER_HEADING = "# ========== 扩展注入层： =========="
"""预览里给扩展那一层起的小标题（调试页的分段索引按它列一段）。"""

# 检索流水线：整条动作（多条查询 + 读正文 + 补查）一共最多花这么多秒
SEARCH_BUDGET_SECONDS = 20.0
# 读回来的正文按链接缓存这么久，避免同一篇被反复抓
READ_CACHE_SECONDS = 6 * 3600
# 一篇正文最多留多少字给模型看
READ_PASSAGE_CHARS = 1200

# 「自上次回复以来收到的图」最多留几张：多留几张是为了让超出内联上限的旧图
# 还能被转述成文字（只留上限张数的话，多出来的图除了丢掉没有别的出路）
PENDING_IMAGE_KEEP = 12
# 调试回显的防抖窗口（秒）：同一工具的连续调用（例如一次检索并行查 4 条）
# 只发第一条，别把群刷成一排一样的行
DEBUG_ECHO_DEBOUNCE_SECONDS = 2.5
_DEBUG_ECHO_TYPES = ("tool_call", "tool_result", "command_call", "command_result")
# 只给排查用的类型：选「完整」才发到群里，选「精简」只留在日志页
DEBUG_ONLY_ECHO_TYPES = ("incoming",)
# 没写意图、也没配主题/模板时的兜底搜索主题（见 PromptBuilder 里那段说明）
DEFAULT_SEARCH_TOPIC = "今天有什么新鲜事"
# 「合并重启」：她还在调模型时又来了新消息，就把几条并成一次请求重新发起。
# 只在**还没开始发送**的窗口里做，所以不会出现"说了半句又改口"。
MERGE_WINDOW_SECONDS = 5.0
# 「合并重启」的次数上限与一次能并进来的条数上限
MERGE_MAX_RESTARTS = 2
MERGE_MAX_TEXTS = 8
# 连续打断的保护：同一个人连着打断太多次时，先让她把这一轮说完，
# 免得刷屏的人让她永远开不了口
INTERRUPT_STREAK_MAX = 3
INTERRUPT_STREAK_WINDOW_SECONDS = 180.0
REPLY_INTERRUPTED = "被新消息打断：这一次生成作废"
"""``handle_reply`` 的 ``error`` 标记：这轮被新消息打断，调用方不要再补一句。"""

PROMPT_SNAPSHOT_KEEP = 3
"""留几份"实际发出去的请求"（只读调试用，全在内存里）。"""

REPLY_MUTED = "本小时被动回复已达上限：这一条不回"
"""``handle_reply`` 的 ``error`` 标记：配额用尽，接管但静默（不落回主人格）。"""

REPLY_GUARD_RETRIES = 3
"""扩展判"这一轮是废的"（模型拒答）时最多重问几次；问满就这一轮不出声。"""
# 一轮里最多往群里贴几张工具 / 指令生成的图（批量出图时不至于刷屏）
MAX_GROUP_IMAGES = 9
# 事件掷骰最多按这么久折算概率：tick 被拖慢时不该一次补出"必中"
EVENT_ROLL_MAX_GAP_HOURS = 0.25
# 最近一次检索记在 kv 里的键前缀与有效期（提示词里提醒她"刚查过什么"）
SEARCH_LOG_KEY = "search.last"
SEARCH_LOG_MINUTES = 60
# 好奇高过这条线时，除了上网查，也会想去问人（和 decider 里"好奇→搜索"用同一条线）
ASK_ABOUT_CURIOSITY = 0.7

OUTDOOR_HINT_HOURS = 10.0
"""在家待满这么久（或者压根没出过门）就提醒她一句"可以出去走走"。"""
# 别人的负面口吻在她这儿有多重，按**亲密度**缩放：路人几乎不往心里去，
# 特别的人一句话顶别人几句。曲线是 `floor + (ceil - floor) * (档位比例 ^ 1.6)`，
# 七档时的实际值大约是：敌意 0.20 / 冷淡 0.27 / 客气 0.42 / 熟人 0.63 /
# 朋友 0.88 / 亲近 1.17 / 特别的人 1.50。
TONE_INTIMACY_FLOOR = 0.2
TONE_INTIMACY_CEIL = 1.15
"""负面口吻按亲密度放大到几倍：路人 0.2，最亲近的人 1.15。

以前是 1.5——配合"一句话吃三笔账"（口吻脉冲 + 基线漂移 + 模型自评），
特别亲近的人一句重话能掉 0.20 效价，冷脸挂半天，聊天体验很差。
"""

NEGATIVE_VALENCE_ROUND_CAP = 0.10
"""一轮对话里，负面情绪**最多**把效价压低这么多（含基线连带的那部分）。"""

NEGATIVE_VALENCE_ROUND_WINDOW = 30.0
"""两条负面在这么多秒内算"同一轮"（回复管线里前后脚发生）。"""

NEGATIVE_TONE_FRESH_HOURS = 24.0
"""连着被伤超过这么久就不再递减（新的账重新从"最疼"算起）。"""

NEGATIVE_TONE_STREAK_SCALE = (1.0, 0.6, 0.35)
"""连着被伤时每一笔的倍率：第一次最疼，之后越来越麻木。"""

NEGATIVE_VALENCE_LOW_LINE = 0.45
"""效价已经低于这条线（正在冷脸）：这一轮不再往下压，只记一笔账。"""

DESIRE_TEASE_FLOOR = 0.3
DESIRE_TEASE_CEIL = 1.6
"""被撩一下的加成倍数：路人撩她几乎没用，特别的人撩一下跳得最明显。"""
# 生成搜索关键词时的额外要求：不写清楚，模型会给你一个"什么都要"的万能查询
SEARCH_QUERY_RULES = (
    "这次要填的是**搜索关键词**：写成能直接丢进搜索框的词（谁 / 什么时候 / 哪方面），"
    "20 字以内；只查一件事，不要罗列多个主题，"
    "不要写成「获取……的最新信息/实时更新」这种句子；"
    "最近聊天里提到过的话题优先。"
)
# 写记忆时模型偶尔会"反过来问人名"。出现这些字样就不是记忆，改用兜底文案。
MEMORY_REFUSAL_HINTS = (
    "捏造",
    "没有出现对方",
    "没出现对方",
    "对方是谁",
    "对方的名字",
    "告诉我对方",
    "请告诉",
    "需要你告诉",
    "无法确定对方",
    "给不出",
)
