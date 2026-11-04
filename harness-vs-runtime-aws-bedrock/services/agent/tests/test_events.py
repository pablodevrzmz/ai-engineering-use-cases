"""agent.py: the turn runner's event order, the error path and turn serialisation, with a scripted agent."""

from __future__ import annotations

import asyncio

import pytest

from app.agent import Session, ToolEventsHook, make_emitter, run_turn
from tests.fakes import TOOL, USAGE, FakeAgent


def make_session(script: list[tuple]) -> Session:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    emit = make_emitter(loop, queue)
    agent = FakeAgent(script, ToolEventsHook(emit))
    return Session(runtime_session_id="s-" + "0" * 34, agent=agent, queue=queue, emit=emit)


async def collect(session: Session, prompt: str) -> list[dict]:
    return [event async for event in run_turn(session, prompt)]


@pytest.mark.asyncio
async def test_run_turn_orders_text_tool_and_done_with_usage() -> None:
    session = make_session(
        [
            ("text", "Let me check."),
            ("reasoning", "planning..."),
            ("tool", TOOL, "tu-1", {"query": "latest terraform release", "maxResults": 5}, True),
            ("text", " Terraform 1.16 is current."),
        ]
    )

    events = await collect(session, "what is the latest terraform?")

    assert events == [
        {"type": "text", "delta": "Let me check."},
        {"type": "tool", "name": TOOL, "status": "start", "toolUseId": "tu-1", "input": {"query": "latest terraform release", "maxResults": 5}},
        {"type": "tool", "name": TOOL, "status": "end", "toolUseId": "tu-1", "ok": True},
        {"type": "text", "delta": " Terraform 1.16 is current."},
        {"type": "done", "stopReason": "end_turn", "usage": USAGE},
    ]
    assert session.agent.prompts == ["what is the latest terraform?"]


@pytest.mark.asyncio
async def test_run_turn_reports_failed_tool_and_agent_error() -> None:
    session = make_session([("text", "Hi"), ("tool", TOOL, "tu-9", {"query": "x"}, False), ("raise", RuntimeError("model unavailable"))])

    events = await collect(session, "hello")

    assert events == [
        {"type": "text", "delta": "Hi"},
        {"type": "tool", "name": TOOL, "status": "start", "toolUseId": "tu-9", "input": {"query": "x"}},
        {"type": "tool", "name": TOOL, "status": "end", "toolUseId": "tu-9", "ok": False},
        {"type": "error", "code": "AGENT_ERROR", "message": "model unavailable"},
    ]
    assert not session.lock.locked()


@pytest.mark.asyncio
async def test_run_turn_serialises_turns_on_one_session() -> None:
    session = make_session([("text", "a"), ("text", "b")])
    first, second = await asyncio.gather(collect(session, "p1"), collect(session, "p2"))
    assert [e["type"] for e in first] == ["text", "text", "done"]
    assert [e["type"] for e in second] == ["text", "text", "done"]
    assert session.agent.prompts == ["p1", "p2"]


@pytest.mark.asyncio
async def test_empty_text_deltas_are_dropped() -> None:
    session = make_session([("text", ""), ("text", "ok")])
    events = await collect(session, "p")
    assert events[:-1] == [{"type": "text", "delta": "ok"}]
