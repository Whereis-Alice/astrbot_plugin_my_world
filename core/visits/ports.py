"""The federation knows public identities and decisions, never the AstrBot runtime."""
from typing import Any, Protocol

Visit = dict[str, Any]


class VisitActorPort(Protocol):
    async def describe(self, scope: str) -> dict[str, Any]: ...
    async def decide(self, scope: str, phase: str, visit: Visit) -> dict[str, Any]: ...
    async def remember(self, scope: str, visit: Visit) -> None: ...
