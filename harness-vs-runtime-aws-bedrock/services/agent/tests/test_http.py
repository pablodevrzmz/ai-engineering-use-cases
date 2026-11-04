"""main.py: the AgentCore HTTP contract (/ping, /invocations), session reuse by header and SSE framing."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import agent as agent_module
from app.main import SESSION_HEADER, create_app, sse
from app.settings import Settings
from tests.fakes import FakeAgent

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def test_sse_frame() -> None:
    assert sse({"type": "text", "delta": "hé\n"}) == b'data: {"type":"text","delta":"h\\u00e9\\n"}\n\n'


def events_of(response) -> list[dict]:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.split("\n\n") if line.strip()]
    assert all(frame.startswith("data: ") for frame in frames)
    return [json.loads(frame[len("data: "):]) for frame in frames]


@pytest.fixture
def client(monkeypatch):
    built: list[dict[str, Any]] = []

    def fake_build_agent(settings, *, tools, hooks):
        built.append({"tools": len(tools), "prompt": settings.system_prompt})
        return FakeAgent([("text", "Hello ")], hooks[0])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)
    monkeypatch.setattr(agent_module, "build_mcp_client", lambda settings: object())  # never connect in tests
    sessions: dict = {}
    app = create_app(Settings(gateway_url="https://gw.example/mcp", system_prompt="be brief"), sessions)
    with TestClient(app) as test_client:
        test_client.built = built
        test_client.sessions = sessions
        yield test_client


def post(client, body, session_id: str | None = SESSION_ID):
    headers = {SESSION_HEADER: session_id} if session_id is not None else {}
    return client.post("/invocations", content=json.dumps(body) if not isinstance(body, str) else body, headers={**headers, "Content-Type": "application/json"})


def test_ping(client) -> None:
    assert client.get("/ping").json() == {"status": "Healthy"}


def test_invocation_streams_events_and_reuses_the_session(client) -> None:
    events = events_of(post(client, {"prompt": "hi"}))
    assert events[0] == {"type": "text", "delta": "Hello "}
    assert events[-1]["type"] == "done" and events[-1]["stopReason"] == "end_turn"
    assert client.built == [{"tools": 1, "prompt": "be brief"}]
    assert list(client.sessions) == [SESSION_ID]

    events_of(post(client, {"prompt": "again"}))  # same header: same session, no second agent built
    assert len(client.built) == 1
    assert client.sessions[SESSION_ID].agent.prompts == ["hi", "again"]


def test_invocation_without_session_or_prompt_is_bad_request(client) -> None:
    assert events_of(post(client, {"prompt": "hi"}, session_id=None))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, {"prompt": ""}))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, {"prompt": ["not", "a", "string"]}))[0]["code"] == "BAD_REQUEST"
    assert events_of(post(client, "not json"))[0]["code"] == "BAD_REQUEST"
    assert client.sessions == {}


def test_no_gateway_means_no_tools(monkeypatch) -> None:
    built: list[int] = []

    def fake_build_agent(settings, *, tools, hooks):
        built.append(len(tools))
        return FakeAgent([("text", "ok")], hooks[0])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)
    with TestClient(create_app(Settings(gateway_url=""), {})) as client:
        assert events_of(post(client, {"prompt": "hi"}))[-1]["type"] == "done"
    assert built == [0]
