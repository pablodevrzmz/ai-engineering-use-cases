"""FastAPI application factory for the Session API.

``create_app`` accepts every external dependency as an optional argument so
tests can pass fakes (moto resources, a fake token verifier, a fake agent
runtime). When an argument is omitted the real client is built lazily in the
lifespan, i.e. at server start, not at import time.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .auth import Caller, TokenVerifier, Unauthorized, require_caller
from .files import FileNotFound, Files, make_s3_client
from .runtime import AgentRuntime, relay, sse
from .settings import Settings
from .threads import MAX_TITLE_CHARS, ThreadNotFound, Threads

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_PROMPT_CHARS = 20_000


class CreateThread(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


def create_app(
    settings: Settings | None = None,
    *,
    verifier: TokenVerifier | None = None,
    ddb_table: Any = None,
    s3: Any = None,
    runtime: Any = None,
) -> FastAPI:
    """Build the app. Keyword arguments are injection points for tests."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Real AWS clients are created here, once, when the server starts.
        # Doing it at import time would make ``import app.main`` need AWS.
        missing = settings.missing()
        if missing and None in (verifier, ddb_table, s3, runtime):
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
        app.state.settings = settings
        app.state.verifier = verifier or TokenVerifier(
            issuer=settings.issuer, audience=settings.client_id, jwks_url=settings.jwks_url
        )
        table = ddb_table or boto3.resource("dynamodb", region_name=settings.region).Table(settings.threads_table)
        app.state.threads = Threads(table, settings.thread_ttl_days)
        app.state.files = Files(
            s3 or make_s3_client(settings.region),
            settings.files_bucket,
            settings.files_ttl_days,
            settings.files_url_ttl_seconds,
        )
        app.state.runtime = runtime or AgentRuntime.from_settings(settings)
        yield

    app = FastAPI(title="Artifacts on AWS: Session API", lifespan=lifespan)
    _register_error_handlers(app)

    # ---- unauthenticated -------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/config.json")
    async def config() -> dict[str, str]:
        """What the page needs to sign in; all of it is public by design."""
        return {"region": settings.region, "userPoolId": settings.user_pool_id, "clientId": settings.client_id}

    # ---- threads ---------------------------------------------------------

    @app.post("/v1/threads", status_code=201)
    async def create_thread(body: CreateThread, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        item = await run_in_threadpool(threads.create, caller.user_id, body.title)
        return Threads.to_api(item)

    @app.get("/v1/threads")
    async def list_threads(
        request: Request,
        caller: Caller = Depends(require_caller),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        threads: Threads = request.app.state.threads
        items = await run_in_threadpool(threads.list_for, caller.user_id, limit)
        return {"threads": [Threads.to_api(item) for item in items]}

    @app.get("/v1/threads/{thread_id}")
    async def get_thread(thread_id: str, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        item = await run_in_threadpool(threads.get_owned, caller.user_id, thread_id)
        return Threads.to_api(item)

    @app.delete("/v1/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: str, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        files: Files = request.app.state.files
        await run_in_threadpool(threads.get_owned, caller.user_id, thread_id)
        # Files first, row second: if the S3 delete fails the row survives and
        # the user can retry. The other order could leave orphans that only
        # the lifecycle rule would ever clean up.
        await run_in_threadpool(files.delete_thread, caller.user_id, thread_id)
        await run_in_threadpool(threads.delete, caller.user_id, thread_id)
        return Response(status_code=204)

    # ---- runs ------------------------------------------------------------

    @app.post("/v1/threads/{thread_id}/runs")
    async def run_thread(thread_id: str, body: RunRequest, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        agent: Any = request.app.state.runtime
        item = await run_in_threadpool(threads.get_owned, caller.user_id, thread_id)
        run_id = secrets.token_hex(8)
        payload = {"prompt": body.prompt, "threadId": thread_id, "runId": run_id}

        async def stream():
            try:
                try:
                    upstream = await asyncio.to_thread(agent.invoke, item["runtime_session_id"], caller.user_id, payload)
                except Exception as exc:
                    # The HTTP status is already 200 once we stream, and the
                    # browser is reading events, so failures travel as events.
                    log.warning("invoke failed for thread %s: %s", thread_id, exc)
                    yield sse({"type": "error", "code": "RUNTIME_UNAVAILABLE", "message": str(exc)})
                    return
                async for chunk in relay(upstream, heartbeat_seconds=settings.heartbeat_seconds):
                    yield chunk
            finally:
                await run_in_threadpool(threads.touch, caller.user_id, thread_id)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # no-cache for intermediaries; X-Accel-Buffering tells proxies that
            # honour it (nginx) not to buffer the stream.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- files -----------------------------------------------------------

    @app.get("/v1/threads/{thread_id}/files")
    async def list_files(thread_id: str, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        files: Files = request.app.state.files
        await run_in_threadpool(threads.get_owned, caller.user_id, thread_id)
        infos = await run_in_threadpool(files.list_thread, caller.user_id, thread_id)
        return {"files": [info.to_api() for info in infos]}

    @app.get("/v1/threads/{thread_id}/files/{file_id}")
    async def describe_file(thread_id: str, file_id: str, request: Request, caller: Caller = Depends(require_caller)):
        threads: Threads = request.app.state.threads
        files: Files = request.app.state.files
        await run_in_threadpool(threads.get_owned, caller.user_id, thread_id)
        info = await run_in_threadpool(files.describe, caller.user_id, thread_id, file_id)
        url, url_expires = await run_in_threadpool(files.download_url, info)
        return JSONResponse(
            {"file": info.to_api(), "url": url, "urlExpiresAt": url_expires.strftime("%Y-%m-%dT%H:%M:%SZ")},
            # The body contains a bearer-like URL; never let a cache keep it.
            headers={"Cache-Control": "no-store"},
        )

    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Every error body is ``{"error": "<code>"}`` so the page handles one shape."""

    @app.exception_handler(Unauthorized)
    async def _unauthorized(_: Request, __: Unauthorized) -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.exception_handler(ThreadNotFound)
    @app.exception_handler(FileNotFound)
    async def _not_found(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"error": "invalid_request", "detail": detail}, status_code=422)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        code = exc.detail if isinstance(exc.detail, str) else "error"
        return JSONResponse({"error": code.lower().replace(" ", "_")}, status_code=exc.status_code, headers=exc.headers)


# uvicorn entry point: ``uvicorn app.main:app``. Settings come from the
# environment; clients are created when the server starts (see lifespan).
app = create_app()
