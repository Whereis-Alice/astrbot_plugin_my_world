"""Settle real clock work and scan a due appointment without advancing time again."""
from __future__ import annotations

import asyncio


async def settled_clock_delivery(engine):
    pending = tuple(getattr(engine, "_life_delivery_jobs", ()))
    await asyncio.wait_for(asyncio.gather(*pending), timeout=5)


async def settled_schedule_outcomes(engine, outcomes):
    await settled_clock_delivery(engine)
    delivery = getattr(engine, "_schedule_delivery_job", None)
    if delivery is not None:
        await asyncio.wait_for(delivery, timeout=5)
    collected = list(outcomes)
    schedule_job = getattr(engine, "_schedule_model_job", None)
    if schedule_job is not None:
        collected.extend(schedule_job.result())
    rows = await engine.run_schedules()
    for outcome in rows:
        await engine._deliver(outcome)
    engine._record_tick(rows)
    collected.extend(rows)
    # Fast schedules can be present in both tick() and its completed model job.
    return list({id(outcome): outcome for outcome in collected}.values())
