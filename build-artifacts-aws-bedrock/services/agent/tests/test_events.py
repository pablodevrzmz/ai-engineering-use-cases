"""agent.py and main.py: the turn runner's event order, the error path, and the HTTP guards.

A scripted FakeAgent replaces the model. It yields the same dict shapes Strands' `stream_async` does, and when the
script says "tool" it drives the real ToolEventsHook through a real HookRegistry and runs the publisher in a worker
thread, the way Strands runs synchronous tools.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

from app import agent as agent_module
from app.agent import Session, ToolEventsHook, make_emitter, run_turn, system_prompt
from app.main import SESSION_HEADER, USER_HEADER, create_app, sse
from app.settings import Settings


class FakeAgent:
    """Replays a script. Steps: ("text", s) · ("reasoning", s) · ("tool", name, id, ok, during) · ("raise", exc)."""

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
                _, name, tool_use_id, ok, during = step
                tool_use = {"name": name, "toolUseId": tool_use_id, "input": {}}
                self.registry.invoke_callbacks(BeforeToolCallEvent(agent=self, selected_tool=None, tool_use=tool_use, invocation_state={}))
                if during is not None:
                    await asyncio.to_thread(during)
                result = {"toolUseId": tool_use_id, "status": "success" if ok else "error", "content": []}
                self.registry.invoke_callbacks(AfterToolCallEvent(agent=self, selected_tool=None, tool_use=tool_use, invocation_state={}, result=result))
            elif kind == "raise":
                raise step[1]
            await asyncio.sleep(0)
        yield {"result": SimpleNamespace(stop_reason=self.stop_reason)}


class FakePublisher:
    def __init__(self, emit) -> None:
        self.emit = emit
        self.runs: list[str] = []

    def begin_run(self, run_id: str) -> None:
        self.runs.append(run_id)

    def publish(self, run_id: str) -> None:  # what publish_file does last, from a worker thread
        self.emit({"type": "file", "fileId": "f" * 32, "name": "fruits.csv", "runId": run_id})


def make_session(script, *, with_publisher: bool = True) -> Session:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    emit = make_emitter(loop, queue)
    hook = ToolEventsHook(emit)
    publisher = FakePublisher(emit) if with_publisher else None
    agent = FakeAgent(script(publisher) if callable(script) else script, hook)
    return Session(runtime_session_id="t1-abc", user_id="u1", thread_id="t1", agent=agent, publisher=publisher, queue=queue, emit=emit)


async def collect(session: Session, prompt: str, run_id: str) -> list[dict]:
    return [event async for event in run_turn(session, prompt, run_id)]


@pytest.mark.asyncio
async def test_run_turn_orders_text_tool_file_and_done() -> None:
    session = make_session(
        lambda publisher: [
            ("text", "Working"),
            ("reasoning", "thinking..."),
            ("tool", "code_interpreter", "tu-1", True, None),
            ("tool", "publish_file", "tu-2", True, lambda: publisher.publish("run-1")),
            ("text", " done."),
        ]
    )

    events = await collect(session, "make a csv", "run-1")

    assert events == [
        {"type": "text", "delta": "Working"},
        {"type": "tool", "name": "code_interpreter", "status": "start", "toolUseId": "tu-1"},
        {"type": "tool", "name": "code_interpreter", "status": "end", "toolUseId": "tu-1", "ok": True},
        {"type": "tool", "name": "publish_file", "status": "start", "toolUseId": "tu-2"},
        {"type": "file", "fileId": "f" * 32, "name": "fruits.csv", "runId": "run-1"},
        {"type": "tool", "name": "publish_file", "status": "end", "toolUseId": "tu-2", "ok": True},
        {"type": "text", "delta": " done."},
        {"type": "done", "runId": "run-1", "stopReason": "end_turn"},
    ]
    assert session.publisher.runs == ["run-1"]
    assert session.agent.prompts == ["make a csv"]


@pytest.mark.asyncio
async def test_run_turn_reports_failed_tool_and_agent_error() -> None:
    session = make_session([("text", "Hi"), ("tool", "code_interpreter", "tu-9", False, None), ("raise", RuntimeError("model unavailable"))])

    events = await collect(session, "hello", "run-2")

    assert events == [
        {"type": "text", "delta": "Hi"},
        {"type": "tool", "name": "code_interpreter", "status": "start", "toolUseId": "tu-9"},
        {"type": "tool", "name": "code_interpreter", "status": "end", "toolUseId": "tu-9", "ok": False},
        {"type": "error", "code": "AGENT_ERROR", "message": "model unavailable"},
    ]
    assert not session.lock.locked()


@pytest.mark.asyncio
async def test_run_turn_serialises_turns_on_one_session() -> None:
    session = make_session([("text", "a"), ("text", "b")], with_publisher=False)
    first, second = await asyncio.gather(collect(session, "p1", "r1"), collect(session, "p2", "r2"))
    assert [e["type"] for e in first] == ["text", "text", "done"] and first[-1]["runId"] == "r1"
    assert [e["type"] for e in second] == ["text", "text", "done"] and second[-1]["runId"] == "r2"
    assert session.agent.prompts == ["p1", "p2"]


def test_system_prompt_mentions_files_only_when_publishing() -> None:
    assert "publish_file" in system_prompt(True)
    assert "publish_file" not in system_prompt(False)
    assert "code_interpreter" in system_prompt(False)


def test_sse_frame() -> None:
    assert sse({"type": "text", "delta": "hé\n"}) == b'data: {"type":"text","delta":"h\\u00e9\\n"}\n\n'


# --- HTTP guards ------------------------------------------------------------------------------------------------------


def events_of(response) -> list[dict]:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.split("\n\n") if line.strip()]
    assert all(frame.startswith("data: ") for frame in frames)
    return [json.loads(frame[len("data: "):]) for frame in frames]


@pytest.fixture
def client(monkeypatch):
    built: list[dict[str, Any]] = []

    def fake_build_agent(settings, *, tools, hooks, publishing):
        built.append({"tools": [t.tool_name for t in tools], "publishing": publishing})
        return FakeAgent([("text", "Hello ")], hooks[0])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)
    sessions: dict = {}
    app = create_app(Settings(files_bucket="files-test"), sessions)
    with TestClient(app) as test_client:
        test_client.built = built
        test_client.sessions = sessions
        yield test_client


def post(client, body: dict, session_id: str | None = "t1-0123456789abcdef", user_id: str | None = "user-1"):
    headers = {}
    if session_id is not None:
        headers[SESSION_HEADER] = session_id
    if user_id is not None:
        headers[USER_HEADER] = user_id
    return client.post("/invocations", json=body, headers=headers)


def test_ping(client) -> None:
    assert client.get("/ping").json() == {"status": "Healthy"}


def test_invocation_streams_events_and_binds_session(client) -> None:
    body = {"prompt": "hi", "threadId": "t1", "runId": "r1"}
    events = events_of(post(client, body))
    assert events == [{"type": "text", "delta": "Hello "}, {"type": "done", "runId": "r1", "stopReason": "end_turn"}]
    assert client.built == [{"tools": ["code_interpreter", "publish_file"], "publishing": True}]
    assert list(client.sessions) == ["t1-0123456789abcdef"]
    assert client.sessions["t1-0123456789abcdef"].user_id == "user-1"

    events_of(post(client, {**body, "runId": "r2"}))  # same session reused, no second agent built
    assert len(client.built) == 1


def test_invocation_without_user_or_session_is_bad_request(client) -> None:
    body = {"prompt": "hi", "threadId": "t1", "runId": "r1"}
    assert events_of(post(client, body, user_id=None))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, body, session_id=None))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, body, user_id="../other"))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, {"prompt": "hi"}))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, {"prompt": "hi", "threadId": "../x", "runId": "r"}))[0]["code"] == "BAD_REQUEST"
    assert client.sessions == {}


def test_invocation_for_another_thread_or_user_is_forbidden(client) -> None:
    body = {"prompt": "hi", "threadId": "t1", "runId": "r1"}
    events_of(post(client, body))  # binds the session to user-1

    other_thread = events_of(post(client, {**body, "threadId": "t2"}))
    assert other_thread == [{"type": "error", "code": "FORBIDDEN_THREAD", "message": "thread does not match this session"}]

    other_user = events_of(post(client, body, user_id="user-2"))
    assert other_user[0]["code"] == "FORBIDDEN_THREAD" and "another user" in other_user[0]["message"]
    assert len(client.built) == 1


def test_publish_tool_is_absent_without_bucket(monkeypatch) -> None:
    built: list[dict[str, Any]] = []

    def fake_build_agent(settings, *, tools, hooks, publishing):
        built.append({"tools": [t.tool_name for t in tools], "publishing": publishing})
        return FakeAgent([("text", "ok")], hooks[0])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)
    sessions: dict = {}
    with TestClient(create_app(Settings(files_bucket=""), sessions)) as client:
        events = events_of(post(client, {"prompt": "hi", "threadId": "t1", "runId": "r1"}))
    assert events[-1]["type"] == "done"
    assert built == [{"tools": ["code_interpreter"], "publishing": False}]
    assert sessions["t1-0123456789abcdef"].publisher is None


@pytest.mark.asyncio
async def test_get_or_create_wires_a_real_strands_agent() -> None:
    """No network: BedrockModel and Agent only build clients and validate tool specs at construction time."""
    sessions: dict[str, Session] = {}
    settings = Settings(files_bucket="files-test", model_id="us.amazon.nova-pro-v1:0")
    session = agent_module.get_or_create(sessions, "t1-abc", "user-1", "t1", settings, asyncio.get_running_loop())

    agent = session.agent
    assert sorted(agent.tool_names) == ["code_interpreter", "publish_file"]
    assert agent.model.config["model_id"] == "us.amazon.nova-pro-v1:0"
    assert agent.model.config["max_tokens"] == 4096 and agent.model.config["temperature"] == 0.2
    assert "cache_config" not in agent.model.config
    assert "publish_file" in agent.system_prompt
    assert agent.hooks.has_callbacks()
    assert agent.callback_handler.__name__ == "null_callback_handler"  # nothing printed to the container's stdout
    assert session.publisher is not None and session.publisher.publish_file.tool_name == "publish_file"
    assert agent_module.get_or_create(sessions, "t1-abc", "user-1", "t1", settings, asyncio.get_running_loop()) is session
