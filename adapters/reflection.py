"""Post-send reflection scheduling, isolated from host hook orchestration."""

from __future__ import annotations

import asyncio
from typing import Any


class ReflectionMixin:
    async def _close_response_tasks(self) -> None:
        """Stop reflection and plugin model requests before persistence is closed."""

        current = asyncio.current_task()
        pending = {task for task in self._reflection_tasks | set(self._self_initiated_tasks)
                   if task is not current and not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._reflection_tasks.clear()
        self._self_initiated_tasks.clear()

    def _spawn_reflection(self, turn: dict[str, Any]) -> None:
        """把回合复盘放到后台：她说完就完事，复盘慢一点不影响聊天。"""

        async def run() -> None:
            try:
                result = await self.engine.reflect_turn(
                    str(turn.get("session_id") or ""),
                    user_id=str(turn.get("user_id") or ""),
                    user_name=str(turn.get("user_name") or ""),
                    user_text=str(turn.get("user_text") or ""),
                    reply_text=str(turn.get("reply") or ""),
                    reasoning_text=str(turn.get("provider_reasoning") or ""),
                    tool_trace=list(turn.get("tools") or []),
                    persona_id=str(turn.get("persona_id") or ""),
                    interaction_entries=list(turn.get("interaction_entries") or []),
                )
                if result and self.debug:
                    inner = str(dict(result.get("reasoning") or {}).get("inner") or "")
                    self.logger.info(
                        f"[my_world] 回合复盘：{result.get('tone') or 'normal'}｜{inner}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug(f"[my_world] 回合复盘失败：{exc}")

        task = asyncio.create_task(run())
        self._reflection_tasks.add(task)
        task.add_done_callback(self._reflection_tasks.discard)
        if len(self._reflection_tasks) > 16:
            for old in list(self._reflection_tasks)[:8]:
                if not old.done():
                    old.cancel()
