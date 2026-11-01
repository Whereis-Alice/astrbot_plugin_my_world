"""The federation knows public identities and decisions, never the AstrBot runtime."""
from typing import Any, Protocol

Visit = dict[str, Any]


class VisitStateError(ValueError):
    """A local state read failed; this says nothing about the neighbour."""

    def __init__(self):
        super().__init__("暂时无法确认当前生活状态")


class VisitActorPort(Protocol):
    async def describe(self, scope: str) -> dict[str, Any]: ...
    async def decide(self, scope: str, phase: str, visit: Visit) -> dict[str, Any]: ...
    async def remember(self, scope: str, visit: Visit) -> None: ...
