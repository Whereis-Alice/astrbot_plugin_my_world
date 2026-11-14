"""Keep an already generated visit choice through cancellation of local verification."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from astrbot.api import logger

from .model_retry import defer_decision
from .protocol import TERMINAL

if TYPE_CHECKING:
    from .flow import VisitFlow


async def preserve_cancelled_candidate(service: VisitFlow, snapshot: dict, cursor: dict,
                                       token: str, candidate: dict | None) -> None:
    """Save only the same paid candidate; never restart inference or confirm a turn."""
    if not isinstance(candidate, dict):
        return

    async def persist() -> None:
        async with service._lock:
            visit = service._state.get("visits", {}).get(snapshot["id"])
            if (not visit or service._closed or not service.config.enabled
                    or visit["status"] in TERMINAL or visit.get("model_pending") != token
                    or visit["scope"] != snapshot["scope"] or visit["local"] != snapshot["local"]):
                return
            visit.pop("model_pending", None)
            if (service._decision_cursor(visit, cursor["phase"]) == cursor
                    and service._life_enabled(visit["scope"])):
                defer_decision(visit, cursor, category="state", now=service._now(),
                               candidate=candidate, waiting=True)
            await service._save()

    task = asyncio.create_task(persist(), name="world-visit-cancelled-candidate-save")
    # The caller is already propagating cancellation. Repeated reload cancellation
    # must still finish this one owned local write before the database is closed.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        task.result()
    except Exception:
        logger.warning("[my_world] 已生成的串门决定未能保存，保留现有恢复状态")


@asynccontextmanager
async def candidate_commit_lock(service: VisitFlow, snapshot: dict, cursor: dict,
                                token: str, candidate: dict | None) -> AsyncIterator[None]:
    """A returned candidate also survives cancellation while waiting to commit."""
    try:
        async with service._lock:
            yield
    except asyncio.CancelledError:
        await preserve_cancelled_candidate(service, snapshot, cursor, token, candidate)
        raise
