"""HTTP surface for Amazon Bedrock AgentCore Runtime: GET /ping and POST /invocations on port 8080.

AgentCore's HTTP protocol contract is small: it health-checks /ping and forwards each InvokeAgentRuntime call as a
POST /invocations with the caller's payload as the body. The runtime session id header
(`x-amzn-bedrock-agentcore-runtime-session-id`) is always present and selects the microVM, so it is also the key
of the in-process Session that holds the Strands agent's conversation.

The response is a plain SSE stream (`data: <json>\\n\\n`). Errors are events in that same stream, not HTTP status
codes, so a client never has to special-case anything.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app import agent as agent_module
from app.agent import Session
from app.settings import Settings

SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


class Invocation(BaseModel):
    """The payload for one user turn. Keep it a string: the model never sees raw JSON from the caller."""

    prompt: str = Field(min_length=1, max_length=20_000)


def sse(event: dict[str, Any]) -> bytes:
    """One SSE frame. JSON never contains a bare newline, so one `data:` line per event is enough."""
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def create_app(settings: Settings | None = None, sessions: dict[str, Session] | None = None) -> FastAPI:
    """Build the FastAPI app. `sessions` is injectable so tests can seed or inspect it."""
    settings = Settings.from_env() if settings is None else settings
    sessions = {} if sessions is None else sessions
    app = FastAPI(title="research agent", docs_url=None, redoc_url=None, openapi_url=None)
    sse_headers = {"Cache-Control": "no-cache"}

    def one_event(event: dict[str, Any]) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield sse(event)

        return StreamingResponse(body(), media_type="text/event-stream", headers=sse_headers)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    async def invocations(request: Request) -> StreamingResponse:
        runtime_session_id = request.headers.get(SESSION_HEADER, "").strip()
        try:
            invocation = Invocation.model_validate(await request.json())
        except (ValidationError, ValueError):
            return one_event({"type": "error", "code": "BAD_REQUEST", "message": "body must be JSON with a non-empty string prompt"})
        if not runtime_session_id:
            return one_event({"type": "error", "code": "BAD_REQUEST", "message": "missing runtime session id"})

        session = agent_module.get_or_create(sessions, runtime_session_id, settings, asyncio.get_running_loop())

        async def body() -> AsyncIterator[bytes]:
            async for event in agent_module.run_turn(session, invocation.prompt):
                yield sse(event)

        return StreamingResponse(body(), media_type="text/event-stream", headers=sse_headers)

    return app


app = create_app()
