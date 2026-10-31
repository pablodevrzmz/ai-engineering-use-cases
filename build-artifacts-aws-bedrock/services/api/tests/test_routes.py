import asyncio
import io
import json
import re

import boto3
import urllib3
from botocore.awsrequest import AWSResponse
from botocore.config import Config

from app.runtime import USER_ID_HEADER, AgentRuntime, relay, sse
from app.threads import runtime_session_id
from tests.conftest import ALICE, FakeBody, FakeRawStream


def collect(agen):
    async def _drain():
        return [chunk async for chunk in agen]

    return asyncio.run(_drain())


# ---- plain routes -------------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_config_json_is_public(client, settings):
    res = client.get("/config.json")
    assert res.status_code == 200
    assert res.json() == {"region": settings.region, "userPoolId": settings.user_pool_id, "clientId": settings.client_id}


def test_index_serves_the_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.text.lower().startswith("<!doctype html>")
    assert "InitiateAuth" in res.text and "/config.json" in res.text


# ---- runs -----------------------------------------------------------------------


def test_run_relays_upstream_sse_verbatim(client, auth, fake_runtime):
    thread_id = client.post("/v1/threads", json={"title": "Fruits"}, headers=auth).json()["threadId"]
    events = sse({"type": "text", "delta": "Here "}) + sse({"type": "file", "fileId": "a" * 32, "name": "f.csv"}) + sse({"type": "done"})
    # Chunks split at arbitrary byte offsets, as a real socket would deliver them.
    fake_runtime.chunks = [events[:7], events[7:40], events[40:]]

    res = client.post(f"/v1/threads/{thread_id}/runs", json={"prompt": "Make a CSV"}, headers=auth)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
    assert res.content == events

    (call,) = fake_runtime.calls
    assert call["runtime_session_id"] == runtime_session_id(thread_id, ALICE)
    assert call["user_id"] == ALICE
    assert call["payload"]["prompt"] == "Make a CSV"
    assert call["payload"]["threadId"] == thread_id
    assert re.fullmatch(r"[0-9a-f]{16}", call["payload"]["runId"])
    assert client.get(f"/v1/threads/{thread_id}", headers=auth).status_code == 200  # touched, still there


def test_run_reports_invoke_failure_as_an_event(client, auth, fake_runtime):
    thread_id = client.post("/v1/threads", json={}, headers=auth).json()["threadId"]
    fake_runtime.error = RuntimeError("endpoint not ready")
    res = client.post(f"/v1/threads/{thread_id}/runs", json={"prompt": "hi"}, headers=auth)
    assert res.status_code == 200
    assert res.content == sse({"type": "error", "code": "RUNTIME_UNAVAILABLE", "message": "endpoint not ready"})


def test_run_validates_prompt_and_thread(client, auth, other_auth, fake_runtime):
    thread_id = client.post("/v1/threads", json={}, headers=auth).json()["threadId"]
    for bad in [{"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 20_001}, {}]:
        res = client.post(f"/v1/threads/{thread_id}/runs", json=bad, headers=auth)
        assert res.status_code == 422, bad
        assert res.json()["error"] == "invalid_request"
    assert client.post(f"/v1/threads/{thread_id}/runs", json={"prompt": "hi"}, headers=other_auth).status_code == 404
    assert fake_runtime.calls == []


# ---- relay --------------------------------------------------------------------------


def test_relay_reads_with_read1_and_closes_the_body():
    raw = FakeRawStream([b"data: {\"a\":1}\n\n", b"data: {\"b\":2}\n\n"])
    body = FakeBody(raw)
    assert collect(relay(body)) == [b"data: {\"a\":1}\n\n", b"data: {\"b\":2}\n\n"]
    assert raw.reads == 3 and body.closed  # two chunks + the EOF read


def test_relay_turns_an_upstream_failure_into_an_error_event():
    body = FakeBody(FakeRawStream([b"data: {\"a\":1}\n\n"], fail_after=1))
    chunks = collect(relay(body))
    assert chunks[0] == b"data: {\"a\":1}\n\n"
    assert chunks[1] == sse({"type": "error", "code": "UPSTREAM_ERROR", "message": "connection reset by peer"})
    assert body.closed


def test_relay_heartbeats_only_between_events():
    # Each read takes 0.12 s; with a 0.05 s heartbeat we must see pings before
    # the first chunk, none while an event is half-sent, and more after it.
    first, second = b"data: {\"a\":", b"1}\n\n"
    chunks = collect(relay(FakeBody(FakeRawStream([first, second], delay=0.12)), heartbeat_seconds=0.05))
    assert b": ping\n\n" in chunks[: chunks.index(first)]
    assert chunks[chunks.index(first) + 1] == second
    assert b": ping\n\n" in chunks[chunks.index(second) :]


# ---- AgentRuntime: header injection and the 409 retry, with botocore but no network ----


def _canned(status, body, headers):
    raw = urllib3.response.HTTPResponse(body=io.BytesIO(body), status=status, headers=headers, preload_content=False)
    return AWSResponse("https://bedrock-agentcore.us-east-1.amazonaws.com/", status, headers, raw)


def test_invoke_signs_the_user_header_and_retries_409():
    client = boto3.client("bedrock-agentcore", region_name="us-east-1", config=Config(retries={"max_attempts": 0}))
    seen, statuses = [], [409, 200]

    def fake_send(request, **_):
        seen.append(request.headers)
        if statuses.pop(0) == 409:
            return _canned(409, json.dumps({"message": "busy"}).encode(), {"x-amzn-ErrorType": "RetryableConflictException"})
        return _canned(200, sse({"type": "done"}), {"Content-Type": "text/event-stream"})

    # ``before-send`` fires after signing; returning a response skips the network.
    client.meta.events.register("before-send.bedrock-agentcore.InvokeAgentRuntime", fake_send)
    sleeps = []
    runtime = AgentRuntime(client, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/demo-abc123", sleep=sleeps.append)

    body = runtime.invoke("session-" + "0" * 32, ALICE, {"prompt": "hi", "threadId": "t", "runId": "r"})

    assert len(seen) == 2 and len(sleeps) == 1 and 0.4 <= sleeps[0] <= 0.6
    for headers in seen:  # botocore hands prepared headers over as bytes
        assert headers[USER_ID_HEADER] == ALICE.encode()
        assert USER_ID_HEADER.lower().encode() in headers["Authorization"]  # listed in SignedHeaders
    assert collect(relay(body)) == [sse({"type": "done"})]


def test_invoke_gives_up_after_max_attempts():
    client = boto3.client("bedrock-agentcore", region_name="us-east-1", config=Config(retries={"max_attempts": 0}))
    client.meta.events.register(
        "before-send.bedrock-agentcore.InvokeAgentRuntime",
        lambda request, **_: _canned(409, b'{"message":"busy"}', {"x-amzn-ErrorType": "RetryableConflictException"}),
    )
    sleeps = []
    runtime = AgentRuntime(client, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/x", max_attempts=3, sleep=sleeps.append)
    try:
        runtime.invoke("session-" + "0" * 32, ALICE, {"prompt": "hi"})
    except Exception as exc:  # botocore ClientError
        assert exc.response["ResponseMetadata"]["HTTPStatusCode"] == 409
    else:
        raise AssertionError("expected a ClientError")
    assert len(sleeps) == 2
