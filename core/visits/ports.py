"""The federation knows public identities and decisions, never the AstrBot runtime."""
import asyncio
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


async def describe_local(actor_port: VisitActorPort, scope: str, *, timeout_seconds: float) -> dict[str, Any]:
    """Bound a local read without exposing private state errors to a neighbour."""
    try:
        async with asyncio.timeout(timeout_seconds):
            return await actor_port.describe(scope)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise VisitStateError() from error
