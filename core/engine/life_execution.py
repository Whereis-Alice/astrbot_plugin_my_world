"""Durable native execution receipts, independent of conversational events."""
from __future__ import annotations

import asyncio
import copy
from uuid import uuid4

from .. import life_matters as matters
from ..life_execution import block, capability_key
from ..model_call_runtime import safe_error
from ..state import WorldState


class LifeExecutionMixin:
    async def life_capabilities(self, session_id: str) -> dict:
        if self.life_executor is None:
            return {"ready": False, "tools": [], "reason": "尚未接入 AstrBot 原生生活执行环境"}
        return await self.life_executor.capabilities(session_id)

    async def _run_life_step(self, session_id, snapshot, *, world, state_key, persona, capabilities):
        run_id = "le_" + uuid4().hex
        try:
            await self._execute_life_step(session_id, snapshot, world=world, state_key=state_key,
                                          persona=persona, capabilities=capabilities, run_id=run_id)
        finally:
            getattr(self, "_life_runs", {}).pop(run_id, None)

    async def _execute_life_step(self, session_id, snapshot, *, world, state_key, persona, capabilities, run_id):
        key = capability_key(capabilities)
        events = []
        async with self.session_state(session_id) as state:
            item = matters.find(state, snapshot["id"])
            if (not item or item["revision"] != snapshot["revision"] or not self._matter_can_work(state)
                    or not await self._matter_context_current(session_id, world, state_key, persona)):
                return
            step = next((s for s in item["steps"] if s.get("status") != "done"), None)
            if not step or step.get("action") != "native_agent" or step.get("not_before", 0) > self._now():
                return
            if not capabilities.get("ready"):
                block(item, kind="environment", reason=capabilities.get("reason") or "等待独立沙盒", now=self._now(),
                      condition="生活沙盒配置或工具能力变化后自动重试，也可以手动继续", key=key)
                matters.history(item, "environment_wait", item["obstacle"], self._now())
                return
            item.update(status="working", blocker="", obstacle="")
            matters.history(item, "agent_started", "通过原生 Agent 在独立生活环境执行这一小步", self._now())
            revision = item["revision"]
            ledger = {"id": run_id, "step_id": step["id"], "status": "running", "at": self._now(), "events": []}
            item["executions"] = [*item.get("executions", []), ledger][-12:]
            snapshot, step = copy.deepcopy(item), copy.deepcopy(step)
            if not hasattr(self, "_life_runs"):
                self._life_runs = {}
            self._life_runs[run_id] = asyncio.current_task()

        async def record(event):
            entry = {**event, "at": self._now()}
            events.append(entry)
            # Address the ORIGINAL owner directly. A scope switch must not move
            # already executed side effects into another character's state.
            async with self.lock(state_key):
                payload = await self.db.call("get_state", state_key)
                if payload is None:
                    return
                state = WorldState.from_payload(payload, state_key)
                item = matters.find(state, snapshot["id"])
                if item:
                    run = next((r for r in item.get("executions", []) if r["id"] == run_id), None)
                    if run:
                        run["events"] = [*run["events"], entry][-120:]
                        await self.save_state(state)

        async def permitted():
            if not await self._matter_context_current(session_id, world, state_key, persona):
                return False
            if capability_key(await self.life_capabilities(session_id)) != key:
                return False
            async with self.session_state(session_id) as state:
                item = matters.find(state, snapshot["id"])
                if not item or item["revision"] != revision or item["status"] != "working":
                    return False
                choice = self.life_choice(state)
                # New sleep, an important event or a due schedule pre-empts the
                # next tool. An in-flight remote operation remains uncertain.
                return (state.energy >= .4 and not state.current_action and not state.current_plan
                        and (choice["kind"] in {"idle", "matter"} or choice.get("matter_id") == snapshot["id"]))

        result, error, cancelled = None, "", False
        started = self._now()
        try:
            result = await self.life_executor.execute(session_id=session_id, owner=state_key,
                matter=snapshot, step=step, persona=persona, run_id=run_id, record=record, permitted=permitted)
        except asyncio.CancelledError:
            cancelled, error = True, "生活执行被中断；已发出的工具操作可能仍在沙盒运行"
        except Exception as exc:
            error = safe_error(exc) or type(exc).__name__
        successful = [event for event in events if event["kind"] == "tool_end" and event.get("ok")]
        attempted = any(event["kind"] == "tool_start" for event in events)
        ended = {event.get("call_id") for event in events if event["kind"] == "tool_end"}
        unknown = any(event["kind"] == "tool_unknown" or
                      (event["kind"] == "tool_start" and event.get("call_id") not in ended) for event in events)
        current = await self._matter_context_current(session_id, world, state_key, persona)
        # Always finish the original ledger, including stale and cancelled work.
        async with self.lock(state_key):
            payload = await self.db.call("get_state", state_key)
            if payload is None:
                return
            state = WorldState.from_payload(payload, state_key)
            item = matters.find(state, snapshot["id"])
            if item is None:
                return
            ledger = next(row for row in item["executions"] if row["id"] == run_id)
            valid = current and item["revision"] == revision and item["status"] == "working"
            ledger.update(status="unknown" if unknown else "partial_failed" if error and successful else
                          "failed" if error else "returned", ended_at=self._now(), error=error,
                          accepted=valid, summary=result.summary if result else "")
            if result:
                ledger["artifacts"] = result.artifacts
            if not valid:
                ledger["status"] = "stale_" + ledger["status"]
                # A stale receipt is retained without overwriting human edits.
                # Execution can never silently replay after resume/target edit.
                if attempted or unknown:
                    item["execution_review_required"] = True
            else:
                minutes = max(0, (self._now() - started) / 60)
                item["worked_minutes"] = item.get("worked_minutes", 0) + minutes
                if unknown or (error and attempted):
                    block(item, kind="uncertain", reason=error or "部分工具执行结果未知，不能自动重放", now=self._now(),
                          condition="核对沙盒与执行记录后，再明确允许后续小步", key=key)
                    item["execution_review_required"] = True
                elif error:
                    block(item, kind="transient", reason=error, now=self._now(), condition="稍后退避重试", key=key)
                elif result and result.outcome == "step_done" and successful:
                    receipt = {"ok": True, "kind": "native_agent", "run_id": run_id, "result": result.summary,
                               "artifacts": result.artifacts, "tool_receipts": successful, "at": self._now()}
                    matters.complete_step(state, item["id"], step["id"], now=self._now(), receipt=receipt)
                    self.memory.remember(session_id=item["session"], persona_id="", node_id=state.node_id,
                        content=f"（生活沙盒的实际操作，仅证明收据结果）{item['text']}：{result.summary}",
                        memory_type="scene", source="life_matter", participants=[item["who"]] if item.get("who") else [])
                    ledger["status"] = "succeeded"
                elif result and result.outcome == "interrupted":
                    item.update(status="active", next_step=step["text"])
                else:
                    block(item, kind="materials" if result and result.condition else "capability",
                          reason=(result.obstacle or result.summary) if result else "没有可验证的工具结果", now=self._now(),
                          condition=result.condition if result else "等待真实执行依据", key=key)
                if attempted and ledger["status"] != "succeeded":
                    # A normal waiting/blocked return can still have written a
                    # file or changed an external service. Resume must not replay
                    # the unfinished step merely because no exception was raised.
                    item["execution_review_required"] = True
                    if ledger["status"] == "returned":
                        ledger["status"] = "partial_waiting"
                    item["recovery_condition"] = "已尝试工具，先核对实际结果，再安排余下小步；" + str(item.get("recovery_condition") or "")[:240]
                if result and result.artifacts:
                    item["artifacts"] = [*item.get("artifacts", []), *result.artifacts][-20:]
                matters.history(item, "agent_result", error or (result.summary if result else "未得到执行结果"), self._now(), run_id=run_id)
            matters.sync(state)
            await self.save_state(state)
        if cancelled:
            raise asyncio.CancelledError
