"""Keep temporary request parts on SDKs whose public helper drops them."""
from __future__ import annotations

import inspect


async def run_agent(context, *, event, chat_provider_id, prompt=None,
                    image_urls=None, audio_urls=None, extra_user_content_parts=None,
                    tools=None, system_prompt=None, contexts=None, max_steps=1,
                    tool_call_timeout=120, agent_context=None, **options):
    helper = context.tool_loop_agent
    arguments = dict(event=event, chat_provider_id=chat_provider_id, prompt=prompt,
        image_urls=image_urls, audio_urls=audio_urls,
        extra_user_content_parts=extra_user_content_parts, tools=tools,
        system_prompt=system_prompt, contexts=contexts, max_steps=max_steps,
        tool_call_timeout=tool_call_timeout, agent_context=agent_context, **options)
    if not extra_user_content_parts or "extra_user_content_parts" in inspect.signature(helper).parameters:
        return await helper(**arguments)

    # AstrBot 4.28.2 accepts these parts through **kwargs but sends them only
    # to runner.reset(), which ignores them. Use the same native runner with
    # a complete ProviderRequest; never flatten media or temporary flags.
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    provider = await context.provider_manager.get_provider_by_id(chat_provider_id)
    if provider is None:
        raise RuntimeError(f"Provider {chat_provider_id} not found")
    request = ProviderRequest(prompt=prompt, image_urls=image_urls or [],
        audio_urls=audio_urls or [], extra_user_content_parts=extra_user_content_parts,
        func_tool=tools, system_prompt=system_prompt or "", session_id=event.unified_msg_origin,
        contexts=[row.model_dump() if hasattr(row, "model_dump") else row for row in contexts or []])
    if tools and tools.get_tool("astrbot_file_read_tool"):
        from astrbot.core.utils.astrbot_path import get_astrbot_system_tmp_path
        options.setdefault("tool_result_overflow_dir", get_astrbot_system_tmp_path())
        options.setdefault("read_tool", tools.get_tool("astrbot_file_read_tool"))
    runner = ToolLoopAgentRunner()
    await runner.reset(provider=provider, request=request,
        run_context=AgentContextWrapper(
            context=agent_context or AstrAgentContext(context=context, event=event),
            tool_call_timeout=tool_call_timeout),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=options.pop("agent_hooks", None) or BaseAgentRunHooks(),
        streaming=options.pop("stream", False), **options)
    async for _ in runner.step_until_done(max_steps):
        pass
    response = runner.get_final_llm_resp()
    if response is None:
        raise RuntimeError("Agent did not produce a final LLM response")
    return response
