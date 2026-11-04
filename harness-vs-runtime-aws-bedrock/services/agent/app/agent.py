"""The Strands agent, one per runtime session, and the turn runner that turns its activity into stream events.

This file is the Runtime flavor's answer to infra/platform/harness.tf: the model, the prompt, the single tool and
the loop, written out instead of declared. What the harness gives you for free is visible here as code: the MCP
client that signs gateway calls with the container's role, the hook that reports tool calls, the per-session
agent, and the queue that orders text and tool events into one stream.

Why a queue instead of mapping `stream_async` directly: `Agent.stream_async` yields model output (text deltas and
the final result) but is silent while a tool runs, and Strands' hooks fire from the tool's thread. Both drop
events into the same asyncio queue the turn runner drains, so text and tool events come out in the order they
happened. `loop.call_soon_threadsafe` makes the drop safe from a thread.

Why one Session per runtime session id: AgentCore Runtime gives each session its own microVM and routes every
invocation with that id to it, so a process-level dict is the natural home for the agent's conversation. When the
session ends, the microVM (and the dict) go with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from app.settings import Settings

logger = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]


def build_model(settings: Settings) -> BedrockModel:
    """The Bedrock model behind the agent; the same numbers as the harness's bedrock_model_config."""
    return BedrockModel(model_id=settings.model_id, region_name=settings.aws_region, max_tokens=4096, temperature=0.2)


def build_mcp_client(settings: Settings) -> MCPClient:
    """An MCP client for the AgentCore Gateway, signing every request with the container's IAM role (SigV4).

    `aws_iam_streamablehttp_client` is the streamable-HTTP transport from mcp-proxy-for-aws with SigV4 added; the
    signing scope must be the gateway's service, "bedrock-agentcore". Nothing connects until the agent first
    needs the tools.
    """
    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=settings.gateway_url,
            aws_service="bedrock-agentcore",
            aws_region=settings.aws_region,
        )
    )


def build_agent(settings: Settings, *, tools: list[Any], hooks: list[HookProvider]) -> Agent:
    """A Strands Agent with no console printing (callback_handler=None); the stream is the only output channel."""
    return Agent(
        model=build_model(settings),
        system_prompt=settings.system_prompt,
        tools=tools,
        hooks=hooks,
        callback_handler=None,
    )


class ToolEventsHook(HookProvider):
    """Turns Strands' per-tool hook events into `tool` stream events, so the client can show the search as it happens.

    The tool name arrives as the gateway exposes it, "<target>___WebSearch", and `input` carries the model's
    arguments (the query), which is what a chat wants to print.
    """

    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    def _before(self, event: BeforeToolCallEvent) -> None:
        self._emit(
            {
                "type": "tool",
                "name": event.tool_use["name"],
                "status": "start",
                "toolUseId": event.tool_use["toolUseId"],
                "input": event.tool_use.get("input") or {},
            }
        )

    def _after(self, event: AfterToolCallEvent) -> None:
        result = event.result if isinstance(event.result, dict) else {}
        self._emit(
            {
                "type": "tool",
                "name": event.tool_use["name"],
                "status": "end",
                "toolUseId": event.tool_use["toolUseId"],
                "ok": result.get("status") == "success",
            }
        )


def make_emitter(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> Emit:
    """An `emit` callable that is safe from any thread: it hands the put to the loop that owns the queue."""

    def emit(event: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:  # the loop is closing; nobody is listening any more
            logger.debug("dropped event after loop shutdown: %r", event)

    return emit


@dataclass
class Session:
    """Everything that lives as long as one AgentCore runtime session."""

    runtime_session_id: str
    agent: Any  # strands.Agent, or a stand-in exposing stream_async(prompt)
    queue: asyncio.Queue
    emit: Emit
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def get_or_create(sessions: dict[str, Session], runtime_session_id: str, settings: Settings, loop: asyncio.AbstractEventLoop) -> Session:
    """The Session for a runtime session id, built on first sight.

    The MCP client goes in as a tool provider: Strands starts it and lists the gateway's tools when the agent first
    needs them, and the same connection serves every turn of the session.
    """
    session = sessions.get(runtime_session_id)
    if session is not None:
        return session

    queue: asyncio.Queue = asyncio.Queue()
    emit = make_emitter(loop, queue)
    tools: list[Any] = [build_mcp_client(settings)] if settings.search_enabled else []
    agent = build_agent(settings, tools=tools, hooks=[ToolEventsHook(emit)])
    session = Session(runtime_session_id=runtime_session_id, agent=agent, queue=queue, emit=emit)
    sessions[runtime_session_id] = session
    logger.info("session %s created (search %s)", runtime_session_id, "on" if settings.search_enabled else "off")
    return session


_END = object()
"""Sentinel the pump pushes last, so the drain loop knows the turn is over whatever happened."""


def usage_of(result: Any) -> dict[str, int] | None:
    """Token usage accumulated over the turn, in the same keys the harness reports in its `metadata` event."""
    usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
    if not usage:
        return None
    return {key: int(usage[key]) for key in ("inputTokens", "outputTokens", "totalTokens") if key in usage}


async def _pump(session: Session, prompt: str) -> None:
    """Drive the agent and translate its stream; always ends with the sentinel."""
    emit = session.emit
    try:
        async for event in session.agent.stream_async(prompt):
            if "data" in event and not event.get("reasoning"):
                if event["data"]:
                    emit({"type": "text", "delta": event["data"]})
            elif "result" in event:
                result = event["result"]
                done: dict[str, Any] = {"type": "done", "stopReason": getattr(result, "stop_reason", None)}
                usage = usage_of(result)
                if usage:
                    done["usage"] = usage
                emit(done)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("turn failed in session %s", session.runtime_session_id)
        emit({"type": "error", "code": "AGENT_ERROR", "message": str(exc) or exc.__class__.__name__})
    finally:
        emit(_END)


async def run_turn(session: Session, prompt: str) -> AsyncIterator[dict[str, Any]]:
    """One user turn as an ordered stream of event dicts (text, tool, done or error).

    The session lock serialises turns: a Strands agent holds one conversation and must not be driven twice at once.
    """
    async with session.lock:
        while not session.queue.empty():  # leftovers from a turn whose client went away
            session.queue.get_nowait()
        pump = asyncio.create_task(_pump(session, prompt))
        try:
            while True:
                event = await session.queue.get()
                if event is _END:
                    break
                yield event
        finally:
            if not pump.done():
                pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
