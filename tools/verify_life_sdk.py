"""Offline contract replay against an installed AstrBot SDK; no server is used.

Run from a disposable working directory, with this repository and the SDK on
PYTHONPATH. The native runner and native tool executor run unmodified; only the
provider and sandbox transport are synthetic. This does not test a real VM.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("mw_life_sdk")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
adapter = importlib.import_module("mw_life_sdk.adapters.life_agent")
models = importlib.import_module("mw_life_sdk.core.models")
from astrbot.core.agent.tool import FunctionTool, ToolSet  # noqa: E402
from astrbot.core.provider.entities import LLMResponse, TokenUsage  # noqa: E402
from astrbot.core.tools.computer_tools import FileDownloadTool, FileUploadTool, LocalExecuteShellTool  # noqa: E402


async def main():
    host_config = {"provider_settings": {"computer_use_runtime": "local", "computer_use_require_admin": True,
                   "sandbox": {"booter": "shipyard_neo", "shipyard_neo_endpoint": "http://synthetic.invalid"}}}
    original = copy.deepcopy(host_config)
    files, events, calls, records, sessions = {}, [], [], [], []
    path = "my_world/lm_test/validate.py"

    class FS:
        async def read_file(self, path, **kwargs):
            if path not in files:
                raise FileNotFoundError(path)
            return {"success": True, "path": path, "content": files[path]}

    class Shell:
        async def exec(self, command, **kwargs):
            # Synthetic transport: it neither executes host shell nor accesses
            # real files. Native ExecuteShellTool still parses the actual result.
            calls.append(command)
            files[path] = "# SYNTHETIC FIXTURE ONLY\nprint('3 synthetic tests passed')\n"
            return {"success": True, "exit_code": 0, "stdout": "3 synthetic tests passed", "stderr": ""}

    closed = []

    async def shutdown(*, delete_sandbox=False):
        closed.append(delete_sandbox)

    sandbox = types.SimpleNamespace(fs=FS(), shell=Shell(), shutdown=shutdown)

    async def get_booter(context, session_id):
        cfg = context.get_config(umo=session_id)
        assert cfg["provider_settings"]["computer_use_runtime"] == "sandbox"
        assert session_id.startswith("my_world_life:")
        assert cfg["provider_settings"]["computer_use_require_admin"] is False
        sessions.append(session_id)
        from astrbot.core.computer.computer_client import session_booter
        session_booter[session_id] = sandbox
        return sandbox

    unsafe = FunctionTool(name="host_write", description="untrusted host write", parameters={"type": "object", "properties": {}})

    class Manager:
        def __init__(self):
            self.native = {}

        def get_builtin_tool(self, kind):
            if kind not in self.native:
                self.native[kind] = kind()
            return self.native[kind]

        def get_full_tool_set(self):
            return ToolSet([unsafe, FileUploadTool(), FileDownloadTool(), LocalExecuteShellTool()])

    manager = Manager()

    class Provider:
        provider_config = {"id": "synthetic", "max_context_tokens": 0}
        model_name = "synthetic"

        def meta(self):
            return types.SimpleNamespace(id="synthetic", type="openai_chat_completion", model="synthetic")

        def get_model(self):
            return "synthetic"

        async def text_chat(self, **kwargs):
            offered = kwargs["func_tool"].tools
            assert unsafe not in offered
            assert not any(isinstance(t, (FileUploadTool, FileDownloadTool, LocalExecuteShellTool)) for t in offered)
            assert kwargs["session_id"].startswith("my_world_life:")
            if not calls:
                return LLMResponse("assistant", tools_call_name=["astrbot_execute_shell"], tools_call_args=[{"command": "write synthetic fixture and test"}], tools_call_ids=["fixture_call"],
                                   usage=TokenUsage(input_other=100, input_cached=0, output=20))
            return LLMResponse("assistant", completion_text=json.dumps({"outcome": "step_done", "summary": "synthetic样例通过，未取得真实素材", "artifacts": [path]}),
                               usage=TokenUsage(input_other=80, input_cached=0, output=30))

    provider = Provider()

    class DB:
        async def call(self, method, *args, **kwargs):
            assert method == "append_model_call", method
            records.append(args[0])

    class Host:
        provider_manager = types.SimpleNamespace(get_provider_by_id=None)
        persona_manager = None

        def get_config(self, **kwargs):
            assert kwargs.get("umo") == "qq:GroupMessage:42", kwargs
            return host_config

        def get_llm_tool_manager(self):
            return manager

        def get_provider_by_id(self, provider_id):
            return provider

        async def get_current_chat_provider_id(self, session_id):
            assert session_id == "qq:GroupMessage:42", session_id
            return "synthetic"

    async def by_id(provider_id):
        return provider

    Host.provider_manager.get_provider_by_id = by_id
    execution_config = models.LifeExecutionConfig(max_steps=3)
    budget_calls = []

    async def budgeted(channel, **kwargs):
        budget_calls.append(1)
        return await channel.generate(**kwargs)

    plugin = types.SimpleNamespace(context=Host(), engine=types.SimpleNamespace(
        world=types.SimpleNamespace(life_execution=execution_config), _budgeted_generate=budgeted,
        main_session=lambda session: "qq:GroupMessage:42"),
        db=DB(), llm_provider_id="", set_self_initiated=lambda: 1, reset_self_initiated=lambda token: None)
    agent = adapter.AstrBotLifeAgent(plugin)
    cap = await agent.capabilities("qq:GroupMessage:42")
    assert cap["ready"], cap
    assert next(t for t in cap["tools"] if t["name"] == "host_write")["allowed"] is False
    shared_cap = await agent.capabilities("qq:GroupMessage:another-group")
    assert shared_cap == cap, (cap, shared_cap)
    for t in cap["tools"]:
        if t["name"] in {"astrbot_file_upload", "astrbot_file_download"}:
            assert not t["allowed"]
    execution_config.trusted_tools[unsafe.name] = adapter.fingerprint(unsafe)
    trusted = await agent.capabilities("qq:GroupMessage:42")
    assert next(t for t in trusted["tools"] if t["name"] == unsafe.name)["allowed"]
    description = unsafe.description
    unsafe.description = "changed definition requires a new trust grant"
    changed = await agent.capabilities("qq:GroupMessage:42")
    assert not next(t for t in changed["tools"] if t["name"] == unsafe.name)["allowed"]
    unsafe.description = description
    execution_config.trusted_tools.clear()
    native_before = {name: tool.active for name, tool in manager.native.items()}

    async def record(event):
        events.append(event)

    async def permitted():
        return True

    with patch("astrbot.core.computer.computer_client.get_booter", get_booter), patch("astrbot.core.tools.computer_tools.shell.get_booter", get_booter):
        result = await agent.execute(session_id="qq:GroupMessage:another-group", owner="character", matter={"id": "lm_test", "text": "准备素材校验脚本"},
                    step={"text": "仅实现脚本与样例验证"}, persona="原有人设不变", run_id="le_fixture", record=record, permitted=permitted)
    assert result.outcome == "step_done", result
    assert len(result.artifacts) == 1, (result, events)
    assert len(calls) == 1, calls
    assert len(budget_calls) == len(records) == 2, (budget_calls, records)
    assert [r["usage"]["total"] for r in records] == [120, 110], records
    assert any(e["kind"] == "tool_start" for e in events), events
    assert any(e["kind"] == "tool_end" and e["ok"] for e in events), events
    assert host_config == original
    assert {name: tool.active for name, tool in manager.native.items()} == native_before
    assert all(s == sessions[0] for s in sessions)
    # A sandbox that never finishes startup must not occupy a life worker forever.
    entered, stopped, finish_boot = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stalled_booter(*args, **kwargs):
        entered.set()
        try:
            await finish_boot.wait()
            return await get_booter(*args, **kwargs)
        finally:
            stopped.set()

    # Shorten only the synthetic fixture; public configuration keeps its bounds.
    execution_config.timeout_seconds = .1
    with patch("astrbot.core.computer.computer_client.get_booter", stalled_booter):
        try:
            await agent.execute(session_id="qq:GroupMessage:42", owner="character", matter={"id": "lm_stall"},
                step={"text": "检查环境"}, persona="原有人设不变", run_id="le_stall", record=record, permitted=permitted)
        except TimeoutError:
            assert entered.is_set() and not stopped.is_set()
        else:
            raise AssertionError("Sandbox startup escaped the life execution timeout")
        # The native boot continues under its occupied slot, then gets disposed.
        # Cancelling get_booter itself could orphan a VM that is still booting.
        finish_boot.set()
        await asyncio.gather(*agent.sandboxes._cleanup_tasks)
    await agent.close()
    assert stopped.is_set() and closed and all(closed)
    print("PASS: native Agent + native shell tool; isolated session/config; no host file tools; 2 per-request usage records; verified synthetic artifact; startup timeout and late cleanup; QQ config unchanged")


if __name__ == "__main__":
    asyncio.run(main())
