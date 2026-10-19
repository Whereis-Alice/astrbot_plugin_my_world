"""Life execution contracts. The core never implements a shell or a tool loop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


def fingerprint(tool: Any) -> str:
    """An approval applies to one tool schema and owner, not just a reusable name."""
    schema = {"name": tool.name, "owner": getattr(tool, "handler_module_path", None),
              "class": f"{type(tool).__module__}.{type(tool).__qualname__}",
              "parameters": tool.parameters, "description": tool.description}
    return hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def capability_key(capabilities: dict) -> str:
    return hashlib.sha256(json.dumps(capabilities, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


@dataclass
class LifeExecutionResult:
    outcome: str = "unknown"
    summary: str = ""
    artifacts: list[dict] = field(default_factory=list)
    obstacle: str = ""
    condition: str = ""


class LifeExecutor(Protocol):
    async def capabilities(self, session_id: str) -> dict: ...

    async def execute(self, *, session_id: str, owner: str, matter: dict, step: dict,
                      persona: str, run_id: str,
                      record: Callable[[dict], Awaitable[None]],
                      permitted: Callable[[], Awaitable[bool]]) -> LifeExecutionResult: ...


def block(item: dict, *, kind: str, reason: str, now: float, condition: str = "", key: str = "") -> None:
    """Conditions wait for change; transport failures retry with bounded backoff."""
    count = int(item.get("failure_count") or 0) + 1 if kind == "transient" else 0
    item.update(status="waiting" if kind in {"transient", "time"} else "blocked",
                obstacle=reason[:300], blocker=kind, recovery_condition=condition[:300],
                capability_key=key, failure_count=count,
                resume_at=now + min(21600, 900 * 2 ** min(5, count - 1)) if count else 0)


def progress_label(item: dict) -> str:
    if not item.get("steps"):
        return "尚未形成可执行步骤"
    done = sum(s.get("status") == "done" for s in item["steps"])
    return f"已验证 {done}/{len(item['steps'])} 步"
