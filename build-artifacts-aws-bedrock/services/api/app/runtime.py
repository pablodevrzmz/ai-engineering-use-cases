"""Invoking the agent on Amazon Bedrock AgentCore Runtime and relaying its stream.

Two things make this file more than a one-line boto3 call:

1. **Who is asking.** AgentCore forwards only allow-listed custom headers to
   the container. We put the user id in one so the agent can bind its session
   to that user. botocore has no parameter for arbitrary headers, so the value
   travels in a ``ContextVar`` and a ``before-sign`` hook writes the header
   (it must be added *before* signing, or SigV4 would reject the request).

2. **Streaming without buffering.** The response body is a botocore
   ``StreamingBody``. Its ``read(n)`` waits until ``n`` bytes arrive or the
   stream ends, which would hold back every small SSE event. The underlying
   urllib3 stream's ``read1(n)`` returns as soon as *any* bytes are available,
   so the relay uses that, in a dedicated thread, and hands chunks to the
   event loop through a queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .settings import Settings

log = logging.getLogger(__name__)

USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id"
BEFORE_SIGN_EVENT = "before-sign.bedrock-agentcore.InvokeAgentRuntime"
CHUNK_SIZE = 65536

# Set around each invoke; read by the before-sign hook. A ContextVar (not a
# global) so concurrent invocations in different threads never mix users.
_current_user: ContextVar[str | None] = ContextVar("agentcore_user_id", default=None)


def sse(event: dict[str, Any]) -> bytes:
    """One Server-Sent Event carrying a JSON object."""
    return b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _inject_user_header(request: Any, **_: Any) -> None:
    user_id = _current_user.get()
    if user_id:
        request.headers[USER_ID_HEADER] = user_id


class AgentRuntime:
    """Thin client for ``InvokeAgentRuntime`` with the header hook and a 409 retry."""

    def __init__(
        self,
        client: Any,
        runtime_arn: str,
        qualifier: str = "live",
        *,
        max_attempts: int = 6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._arn = runtime_arn
        self._qualifier = qualifier
        self._max_attempts = max_attempts
        self._sleep = sleep
        client.meta.events.register(BEFORE_SIGN_EVENT, _inject_user_header)

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentRuntime":
        client = boto3.client(
            "bedrock-agentcore",
            region_name=settings.region,
            config=Config(
                # A run can stream for many minutes; botocore's default 60 s
                # read timeout would cut it off. Retries are disabled because a
                # retried POST would start the agent's turn twice.
                read_timeout=900,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )
        return cls(client, settings.agent_runtime_arn, settings.agent_runtime_qualifier)

    def invoke(self, runtime_session_id: str, user_id: str, payload: dict[str, Any]) -> Any:
        """Start a turn and return the streaming body. Blocking: call it in a thread.

        AgentCore answers HTTP 409 (``RetryableConflictException``) while a
        session's microVM is still starting or busy with the previous turn, so
        that one status is retried with a short exponential backoff.
        """
        token = _current_user.set(user_id)
        try:
            for attempt in range(self._max_attempts):
                try:
                    response = self._client.invoke_agent_runtime(
                        agentRuntimeArn=self._arn,
                        qualifier=self._qualifier,
                        runtimeSessionId=runtime_session_id,
                        contentType="application/json",
                        accept="text/event-stream",
                        payload=json.dumps(payload).encode("utf-8"),
                    )
                    return response["response"]
                except ClientError as exc:
                    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    if status != 409 or attempt == self._max_attempts - 1:
                        raise
                    delay = min(0.4 * 2**attempt, 3.0) + random.uniform(0, 0.2)
                    log.info("agent runtime busy (409); retry %d in %.1fs", attempt + 1, delay)
                    self._sleep(delay)
        finally:
            _current_user.reset(token)
        raise RuntimeError("unreachable")  # pragma: no cover


def _chunk_reader(body: Any) -> Callable[[int], bytes]:
    """Pick the least-buffering read available on a StreamingBody."""
    raw = getattr(body, "_raw_stream", None)
    if raw is not None and hasattr(raw, "read1"):
        return raw.read1
    if raw is not None:
        return raw.read
    return body.read


async def relay(body: Any, *, heartbeat_seconds: float = 15.0) -> AsyncIterator[bytes]:
    """Yield the upstream SSE bytes unchanged, with heartbeats and a final error event.

    The agent already speaks SSE (``data: <json>\\n\\n``), so the API forwards
    bytes verbatim rather than parsing and re-encoding. When the upstream is
    silent (the model is thinking, a tool is running) a ``: ping`` comment
    keeps the connection alive, but only at a line boundary: a comment in the
    middle of a half-sent event would corrupt it.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()

    def pump() -> None:
        try:
            read = _chunk_reader(body)
            while chunk := read(CHUNK_SIZE):
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as exc:  # network drop, read timeout, closed by us
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            _close_quietly(body)
            loop.call_soon_threadsafe(queue.put_nowait, None)

    # A dedicated thread rather than the shared executor: a stream can last
    # 15 minutes, and a handful of them would otherwise exhaust the pool that
    # every other blocking call in the process relies on.
    threading.Thread(target=pump, name="agentcore-relay", daemon=True).start()

    last_byte = b"\n"
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                if last_byte == b"\n":
                    yield b": ping\n\n"
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                log.warning("upstream stream failed: %s", item)
                yield sse({"type": "error", "code": "UPSTREAM_ERROR", "message": str(item)})
                return
            last_byte = item[-1:]
            yield item
    finally:
        # If the browser went away first, closing the body makes the blocked
        # read in the pump thread fail fast instead of lingering until the
        # agent finishes its turn.
        _close_quietly(body)


def _close_quietly(body: Any) -> None:
    try:
        body.close()
    except Exception:  # already closed, or a fake without close()
        pass
