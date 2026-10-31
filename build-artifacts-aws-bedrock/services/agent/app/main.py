"""HTTP surface for Amazon Bedrock AgentCore Runtime: GET /ping and POST /invocations on port 8080.

AgentCore's HTTP protocol contract is small: it health-checks /ping and forwards each InvokeAgentRuntime call as a
POST /invocations with the caller's payload as the body. Two headers matter here. The runtime session id
(`x-amzn-bedrock-agentcore-runtime-session-id`) is always present and selects the microVM. The custom user id
(`x-amzn-bedrock-agentcore-runtime-custom-user-id`) is only forwarded because the Terraform allow-lists it in
`request_header_configuration`; it is set by the Session API from the verified token, which is why this container
can trust it.

The response is a plain SSE stream (`data: <json>\\n\\n`) that the Session API relays byte for byte to the browser.
Errors are events in that same stream, not HTTP status codes, so the relay never has to special-case anything.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app import agent as agent_module
from app.agent import Session
from app.settings import Settings

SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
USER_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-user-id"

ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_USER_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")  # a Cognito `sub` is a UUID; anything path-like is refused


class Invocation(BaseModel):
    """The payload the Session API sends for one user turn."""

    prompt: str = Field(min_length=1, max_length=20_000)
    threadId: str = Field(pattern=ID_PATTERN)
    runId: str = Field(pattern=ID_PATTERN)


def sse(event: dict[str, Any]) -> bytes:
    """One SSE frame. JSON never contains a bare newline, so one `data:` line per event is enough."""
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def create_app(settings: Settings | None = None, sessions: dict[str, Session] | None = None) -> FastAPI:
    """Build the FastAPI app. `sessions` is injectable so tests can seed or inspect it."""
    settings = Settings.from_env() if settings is None else settings
    sessions = {} if sessions is None else sessions
    app = FastAPI(title="artifacts agent", docs_url=None, redoc_url=None, openapi_url=None)
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
        user_id = request.headers.get(USER_HEADER, "").strip()
        try:
            invocation = Invocation.model_validate(await request.json())
        except (ValidationError, ValueError):
            return one_event({"type": "error", "code": "BAD_REQUEST", "message": "body must be JSON with prompt, threadId and runId"})
        if not runtime_session_id or not _USER_ID.match(user_id):
            return one_event({"type": "error", "code": "BAD_REQUEST", "message": "missing runtime session id or user id"})

        # The Session API derives the runtime session id from the thread id, so a body that names another thread
        # cannot borrow this session's sandbox or publish under this thread. The second check closes the other
        # direction: once a session is bound to a user, nobody else may continue it.
        if not runtime_session_id.startswith(f"{invocation.threadId}-"):
            return one_event({"type": "error", "code": "FORBIDDEN_THREAD", "message": "thread does not match this session"})
        existing = sessions.get(runtime_session_id)
        if existing is not None and existing.user_id != user_id:
            return one_event({"type": "error", "code": "FORBIDDEN_THREAD", "message": "session belongs to another user"})

        session = agent_module.get_or_create(
            sessions, runtime_session_id, user_id, invocation.threadId, settings, asyncio.get_running_loop()
        )

        async def body() -> AsyncIterator[bytes]:
            async for event in agent_module.run_turn(session, invocation.prompt, invocation.runId):
                yield sse(event)

        return StreamingResponse(body(), media_type="text/event-stream", headers=sse_headers)

    return app


app = create_app()
