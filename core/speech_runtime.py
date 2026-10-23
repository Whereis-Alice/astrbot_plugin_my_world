"""Context scoped capabilities for one background speech decision."""
from contextvars import ContextVar

SPEECH_REQUEST = ContextVar("world_speech_request", default=None)


class SpeechDraft(str):
    def __new__(cls, text, *, images=(), tools=()):
        value = super().__new__(cls, text)
        value.images = list(images)
        value.tools = list(tools)
        return value


GROUNDING_RULES = (
    "\n# 事实与表达边界\n"
    "人设和世界地点说明属于你自己的设定，不能据此推断对方的住处、天气、行程、身体感受或共同经历。"
    "内心与台词都只依据有来源的消息、生活记录和本轮工具结果；猜测必须保持猜测语气。"
    "没有对方行程或天气资料，就不要补山上风大、海边冷等细节来提醒穿衣；仅仅带了外套并不能证明天气。"
    "一句话只接当前最具体的一点，不复述整个话题，不替对方安排下一步，不把每次接话都写成关心或陪伴宣言。"
    "已经说过的提醒、承诺、提问不换个说法再说；有人明确正在等另一位参与者或另一台机器人的回复时，"
    "记录想法并选择listen，不发‘我也等着’之类占位话。这是现场边界，自然沉默关闭时也适用。"
    "直接找你的正常对话照常回复；这些限制只约束这次后台主动接话。"
)
