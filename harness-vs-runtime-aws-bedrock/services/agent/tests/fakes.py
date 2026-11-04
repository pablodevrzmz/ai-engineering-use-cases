"""A scripted stand-in for the Strands agent, shared by the event and HTTP tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

from app.agent import ToolEventsHook

TOOL = "web-search___WebSearch"
USAGE = {"inputTokens": 1200, "outputTokens": 80, "totalTokens": 1280}


class FakeAgent:
    """Replays a script with the dict shapes Strands' `stream_async` yields.

    Steps: ("text", s) · ("reasoning", s) · ("tool", name, id, input, ok) · ("raise", exc). A "tool" step drives the
    real ToolEventsHook through a real HookRegistry from a worker thread, the way Strands runs synchronous tools.
    """

    def __init__(self, script: list[tuple], hook: ToolEventsHook | None = None, stop_reason: str = "end_turn") -> None:
        self.script = script
        self.stop_reason = stop_reason
        self.registry = HookRegistry()
        if hook is not None:
            hook.register_hooks(self.registry)
        self.prompts: list[str] = []

    async def stream_async(self, prompt: str, **kwargs: Any):
        self.prompts.append(prompt)
        for step in self.script:
            kind = step[0]
            if kind == "text":
                yield {"data": step[1], "delta": {"text": step[1]}}
            elif kind == "reasoning":
                yield {"reasoningText": step[1], "delta": {}, "reasoning": True}
            elif kind == "tool":
                _, name, tool_use_id, tool_input, ok = step
                tool_use = {"name": name, "toolUseId": tool_use_id, "input": tool_input}

                def call_tool() -> None:
                    self.registry.invoke_callbacks(BeforeToolCallEvent(agent=self, selected_tool=None, tool_use=tool_use, invocation_state={}))
                    result = {"toolUseId": tool_use_id, "status": "success" if ok else "error", "content": []}
                    self.registry.invoke_callbacks(AfterToolCallEvent(agent=self, selected_tool=None, tool_use=tool_use, invocation_state={}, result=result))

                await asyncio.to_thread(call_tool)
            elif kind == "raise":
                raise step[1]
            await asyncio.sleep(0)
        yield {"result": SimpleNamespace(stop_reason=self.stop_reason, metrics=SimpleNamespace(accumulated_usage=dict(USAGE)))}
