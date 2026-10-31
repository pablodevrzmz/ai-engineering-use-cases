"""The Strands agent, one per runtime session, and the turn runner that turns its activity into stream events.

Why a queue instead of mapping `stream_async` directly: `Agent.stream_async` yields model output (text deltas and the
final result) but is silent while a tool runs, and tools run in worker threads. The two things the chat most wants
to show during that silence, "running code" and "here is your file", are produced by a hook and by the publish_file
tool respectively. Both drop events into the same asyncio queue the turn runner drains, so text, tool and file
events come out in the order they happened. `loop.call_soon_threadsafe` is what makes the drop safe from a thread:
asyncio.Queue itself is not thread-safe.

Why one Session per runtime session id: AgentCore Runtime gives each session its own microVM and routes every
invocation with that id to it, so a process-level dict is the natural home for the agent's conversation, the sandbox
and the publisher. When the session ends, the microVM (and the dict) go with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel

from app.publish import FilePublisher
from strands.tools.executors import SequentialToolExecutor

from app.sandbox import ExpiringCodeInterpreter
from app.settings import Settings

logger = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

SYSTEM_PROMPT = """You are a helpful assistant with a Python sandbox (the code_interpreter tool) for analysing data, \
doing calculations and creating files. Use it whenever code gives a more reliable answer than guessing. \
Keep answers short and plain. Do not narrate your reasoning or use <thinking> tags; answer directly."""

FILES_PROMPT = """

Files: when the user asks for a file (CSV, Excel, PDF, image, chart, archive ...), write it under the sandbox \
working directory with code, then call publish_file(path, title) once per file, then answer in one or two sentences \
saying what the file contains. Never paste the contents of a file you published into the chat, and never claim a \
file exists before publish_file has returned a result for it."""


def system_prompt(publishing: bool) -> str:
    """The system prompt; the files section is only there when the publish_file tool is actually offered."""
    return SYSTEM_PROMPT + (FILES_PROMPT if publishing else "")


def build_model(settings: Settings) -> BedrockModel:
    """The Bedrock model behind the agent. Low temperature keeps tool arguments (paths, titles) literal."""
    return BedrockModel(model_id=settings.model_id, region_name=settings.aws_region, max_tokens=4096, temperature=0.2)


def build_agent(settings: Settings, *, tools: list[Any], hooks: list[HookProvider], publishing: bool) -> Agent:
    """A Strands Agent with no console printing (callback_handler=None); the stream is the only output channel.

    Tools run sequentially on purpose. Strands executes a turn's tool calls concurrently by default, and a model
    will happily emit "write the file" and "publish the file" in the same turn; running them in order guarantees
    the file exists before publish_file reads it.
    """
    return Agent(
        model=build_model(settings),
        system_prompt=system_prompt(publishing),
        tools=tools,
        hooks=hooks,
        callback_handler=None,
        tool_executor=SequentialToolExecutor(),
    )


class ToolEventsHook(HookProvider):
    """Turns Strands' per-tool hook events into `tool` stream events, so the UI can show "running code".

    Strands 1.56 fires BeforeToolCallEvent and AfterToolCallEvent around every tool execution (strands.hooks.events);
    `tool_use` carries the model's request and `result` the ToolResult dict with its "success"/"error" status.
    """

    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    def _before(self, event: BeforeToolCallEvent) -> None:
        self._emit({"type": "tool", "name": event.tool_use["name"], "status": "start", "toolUseId": event.tool_use["toolUseId"]})

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
    user_id: str
    thread_id: str
    agent: Any  # strands.Agent, or a stand-in exposing stream_async(prompt)
    publisher: FilePublisher | None
    queue: asyncio.Queue
    emit: Emit
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def get_or_create(
    sessions: dict[str, Session],
    runtime_session_id: str,
    user_id: str,
    thread_id: str,
    settings: Settings,
    loop: asyncio.AbstractEventLoop,
) -> Session:
    """The Session for a runtime session id, built on first sight.

    Identity is bound here, from headers the Session API set after verifying the user's token: the model never
    chooses which user or thread a file is stored under. main.py checks that a later request carries the same user.
    """
    session = sessions.get(runtime_session_id)
    if session is not None:
        return session

    queue: asyncio.Queue = asyncio.Queue()
    emit = make_emitter(loop, queue)
    sandbox = ExpiringCodeInterpreter(
        region=settings.aws_region,
        session_timeout_seconds=settings.code_interpreter_session_seconds,
        session_name=f"thread-{thread_id}",
    )
    tools: list[Any] = [sandbox.code_interpreter]
    publisher: FilePublisher | None = None
    if settings.publishing_enabled:
        publisher = FilePublisher(
            sandbox=sandbox,
            s3=boto3.client("s3", region_name=settings.aws_region),
            bucket=settings.files_bucket,
            user_id=user_id,
            thread_id=thread_id,
            ttl_days=settings.files_ttl_days,
            max_bytes=settings.files_max_bytes,
            emit=emit,
        )
        tools.append(publisher.publish_file)
    agent = build_agent(settings, tools=tools, hooks=[ToolEventsHook(emit)], publishing=publisher is not None)
    session = Session(
        runtime_session_id=runtime_session_id,
        user_id=user_id,
        thread_id=thread_id,
        agent=agent,
        publisher=publisher,
        queue=queue,
        emit=emit,
    )
    sessions[runtime_session_id] = session
    logger.info("session %s created for thread %s", runtime_session_id, thread_id)
    return session


_END = object()
"""Sentinel the pump pushes last, so the drain loop knows the turn is over whatever happened."""


class ThinkingFilter:
    """Drop ``<thinking>...</thinking>`` blocks from a stream of text deltas.

    Amazon Nova models narrate their plan inside these tags when they use tools. That text is not meant for the
    user, and because it arrives as streamed deltas a tag can be split across chunks ("<thi" + "nking>"), so the
    filter keeps the shortest tail that could still become a tag and releases it once it cannot.
    """

    OPEN = "<thinking>"
    CLOSE = "</thinking>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._trim_leading = False  # swallow the whitespace the model leaves after a closing tag

    @staticmethod
    def _partial_tag(text: str, tag: str) -> int:
        """Length of the longest suffix of ``text`` that is a proper prefix of ``tag`` (0 when none)."""
        for size in range(min(len(text), len(tag) - 1), 0, -1):
            if tag.startswith(text[-size:]):
                return size
        return 0

    def _release(self, text: str) -> str:
        if self._trim_leading:
            text = text.lstrip()
            if text:
                self._trim_leading = False
        return text

    def feed(self, delta: str) -> str:
        """Return the user-visible part of the stream so far, holding back anything that may be a tag."""
        self._buffer += delta
        visible: list[str] = []
        while self._buffer:
            if self._inside:
                end = self._buffer.find(self.CLOSE)
                if end < 0:
                    keep = self._partial_tag(self._buffer, self.CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
                    break
                self._buffer = self._buffer[end + len(self.CLOSE):]
                self._inside = False
                self._trim_leading = True
                continue
            start = self._buffer.find(self.OPEN)
            if start >= 0:
                visible.append(self._release(self._buffer[:start]))
                self._buffer = self._buffer[start + len(self.OPEN):]
                self._inside = True
                continue
            keep = self._partial_tag(self._buffer, self.OPEN)
            cut = len(self._buffer) - keep
            visible.append(self._release(self._buffer[:cut]))
            self._buffer = self._buffer[cut:]
            break
        return "".join(visible)

    def flush(self) -> str:
        """Release what is left at the end of the turn; an unterminated thinking block is dropped."""
        text = "" if self._inside else self._release(self._buffer)
        self._buffer = ""
        return text


async def _pump(session: Session, prompt: str, run_id: str) -> None:
    """Drive the agent and translate its stream; always ends with the sentinel."""
    emit = session.emit
    thinking = ThinkingFilter()
    try:
        async for event in session.agent.stream_async(prompt):
            if "data" in event and not event.get("reasoning"):
                visible = thinking.feed(event["data"]) if event["data"] else ""
                if visible:
                    emit({"type": "text", "delta": visible})
            elif "result" in event:
                tail = thinking.flush()
                if tail:
                    emit({"type": "text", "delta": tail})
                stop_reason = getattr(event["result"], "stop_reason", None)
                emit({"type": "done", "runId": run_id, "stopReason": stop_reason})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("run %s failed in session %s", run_id, session.runtime_session_id)
        emit({"type": "error", "code": "AGENT_ERROR", "message": str(exc) or exc.__class__.__name__})
    finally:
        emit(_END)


async def run_turn(session: Session, prompt: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    """One user turn as an ordered stream of event dicts (text, tool, file, done or error).

    The session lock serialises turns: a Strands agent holds one conversation and must not be driven twice at once.
    """
    async with session.lock:
        while not session.queue.empty():  # leftovers from a turn whose client went away
            session.queue.get_nowait()
        if session.publisher is not None:
            session.publisher.begin_run(run_id)
        pump = asyncio.create_task(_pump(session, prompt, run_id))
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
