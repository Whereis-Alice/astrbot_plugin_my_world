"""AstrBot 适配层：把宿主能力包成 core/ports.py 里的 Protocol。"""

from __future__ import annotations

from .commands import AstrBotCommands
from .llm import AstrBotLLM
from .messenger import AstrBotMessenger
from .persona import AstrBotPersona
from .tools import AstrBotTools
from .vision import AstrBotVision

__all__ = [
    "AstrBotCommands",
    "AstrBotLLM",
    "AstrBotMessenger",
    "AstrBotPersona",
    "AstrBotTools",
    "AstrBotVision",
]
