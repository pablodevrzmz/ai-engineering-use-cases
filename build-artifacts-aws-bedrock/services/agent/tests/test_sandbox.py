"""sandbox.py: readFiles parsing, the model-facing blob note, and session expiry without a real sandbox.

The session tests replace the AgentCore client class the library instantiates with a fake, so the library's own
`init_session` / `_ensure_session` code paths run for real and the subclass is tested against the behaviour it
depends on (pinned strands-agents-tools 0.8.9).
"""

from __future__ import annotations

import base64
import itertools

import pytest

from app import sandbox as sandbox_module
from app.sandbox import ExpiringCodeInterpreter, SandboxError, parse_read_files, without_blobs
from strands_tools.code_interpreter import agent_core_code_interpreter as library
from strands_tools.code_interpreter.models import ReadFilesAction

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))


def read_files_response(*items: dict, is_error: bool = False, text: str | None = None) -> dict:
    content = list(items)
    if text is not None:
        content.insert(0, {"type": "text", "text": text})
    return {"stream": [{"result": {"isError": is_error, "content": content}}]}


def test_parse_read_files_blob_as_bytes() -> None:
    response = read_files_response({"type": "resource", "resource": {"uri": "file:///chart.png", "mimeType": "image/png", "blob": PNG}})
    file = parse_read_files(response, "chart.png")
    assert file.data == PNG and file.mime_type == "image/png" and file.path == "chart.png"


def test_parse_read_files_blob_as_base64_text() -> None:
    encoded = base64.b64encode(PNG).decode()
    response = read_files_response({"type": "resource", "resource": {"mimeType": "image/png", "blob": encoded}})
    assert parse_read_files(response, "chart.png").data == PNG


def test_parse_read_files_text_is_utf8_encoded() -> None:
    response = read_files_response({"type": "resource", "resource": {"mimeType": "text/csv", "text": "a,b\n1,é\n"}})
    file = parse_read_files(response, "data.csv")
    assert file.data == "a,b\n1,é\n".encode() and file.mime_type == "text/csv"


def test_parse_read_files_is_error() -> None:
    response = read_files_response(is_error=True, text="No such file: nope.csv")
    with pytest.raises(SandboxError, match="nope.csv.*No such file"):
        parse_read_files(response, "nope.csv")


def test_parse_read_files_without_resource_or_result() -> None:
    with pytest.raises(SandboxError, match="not a file"):
        parse_read_files(read_files_response(text="directory listing"), "dir")
    with pytest.raises(SandboxError, match="no result"):
        parse_read_files({"stream": []}, "x")


def test_without_blobs_replaces_binary_with_note() -> None:
    content = [
        {"type": "resource", "resource": {"uri": "file:///a.png", "mimeType": "image/png", "blob": PNG}},
        {"type": "resource", "resource": {"uri": "file:///a.csv", "mimeType": "text/csv", "text": "a,b"}},
    ]
    cleaned = without_blobs(content)
    assert "blob" not in cleaned[0]["resource"]
    assert cleaned[0]["resource"]["text"] == f"<binary file, {len(PNG):,} bytes; use publish_file to give it to the user>"
    assert cleaned[0]["resource"]["mimeType"] == "image/png"
    assert cleaned[1] == content[1]
    assert str(PNG) not in str(cleaned)


# --- session lifetime -------------------------------------------------------------------------------------------------


class FakeClient:
    """Stands in for bedrock_agentcore's CodeInterpreter client: the four calls the library and the subclass make."""

    ids = itertools.count(1)
    started: list["FakeClient"] = []
    fail_invoke = False

    def __init__(self, region=None, session=None) -> None:
        self.region = region
        self.session_id = None
        self.identifier = None
        self.invocations: list[tuple[str, dict]] = []

    def start(self, identifier, name, session_timeout_seconds):
        self.identifier = identifier
        self.session_id = f"aws-session-{next(self.ids)}"
        self.timeout = session_timeout_seconds
        FakeClient.started.append(self)
        return self.session_id

    def get_session(self, interpreter_id=None, session_id=None):
        return {"status": "READY"}

    def invoke(self, method, params=None):
        self.invocations.append((method, params or {}))
        if FakeClient.fail_invoke:
            raise ConnectionError("socket closed")
        if method == "executeCode":
            return {"stream": [{"result": {"isError": False, "structuredContent": {"stdout": "42\n", "stderr": ""}, "content": [{"type": "text", "text": "42"}]}}]}
        if method == "readFiles":
            return read_files_response({"type": "resource", "resource": {"mimeType": "image/png", "blob": PNG}})
        raise AssertionError(method)


class SpyInterpreter(ExpiringCodeInterpreter):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.forgotten: list[str] = []

    def _forget(self, name: str) -> None:
        self.forgotten.append(name)
        super()._forget(name)


@pytest.fixture
def clock():
    state = {"now": 1000.0}
    return state


@pytest.fixture
def interpreter(monkeypatch, clock) -> SpyInterpreter:
    monkeypatch.setattr(library, "BedrockAgentCoreCodeInterpreterClient", FakeClient)
    library._session_mapping.clear()
    FakeClient.started = []
    FakeClient.fail_invoke = False
    yield SpyInterpreter(region="us-east-1", session_timeout_seconds=600, session_name="thread-t1", clock=lambda: clock["now"])
    library._session_mapping.clear()


def test_session_is_created_once_and_reused(interpreter: SpyInterpreter, clock) -> None:
    assert interpreter.file_size("a.txt") == 42
    clock["now"] += 100
    assert interpreter.file_size("a.txt") == 42
    assert len(FakeClient.started) == 1
    client = FakeClient.started[0]
    assert client.timeout == 600 and client.invocations[0][0] == "executeCode"
    assert "getsize('a.txt')" in client.invocations[0][1]["code"] or 'getsize("a.txt")' in client.invocations[0][1]["code"]
    assert library._session_mapping["thread-t1"] == client.session_id
    assert interpreter.forgotten == []


def test_session_is_dropped_before_its_absolute_deadline(interpreter: SpyInterpreter, clock) -> None:
    interpreter.file_size("a.txt")
    first = library._session_mapping["thread-t1"]

    clock["now"] += 600 - sandbox_module.TTL_MARGIN_SECONDS - 1  # one second before the margin: still trusted
    interpreter.file_size("a.txt")
    assert len(FakeClient.started) == 1

    clock["now"] += 1  # at the margin: the cached session is forgotten and a fresh one started
    assert interpreter.file_size("a.txt") == 42
    assert interpreter.forgotten == ["thread-t1"]
    assert len(FakeClient.started) == 2
    assert library._session_mapping["thread-t1"] != first
    assert "thread-t1" in interpreter._sessions


def test_forget_clears_both_caches(interpreter: SpyInterpreter) -> None:
    interpreter.file_size("a.txt")
    interpreter._forget("thread-t1")
    assert "thread-t1" not in interpreter._sessions
    assert "thread-t1" not in library._session_mapping
    assert "thread-t1" not in interpreter._deadlines


def test_transport_failure_forgets_the_session(interpreter: SpyInterpreter) -> None:
    interpreter.file_size("a.txt")
    FakeClient.fail_invoke = True
    with pytest.raises(SandboxError, match="socket closed"):
        interpreter.read_bytes("a.png")
    assert interpreter.forgotten == ["thread-t1"]
    FakeClient.fail_invoke = False
    assert interpreter.read_bytes("a.png").data == PNG  # a fresh session, transparently
    assert len(FakeClient.started) == 2


def test_read_bytes_and_model_facing_read_files(interpreter: SpyInterpreter) -> None:
    file = interpreter.read_bytes("a.png")
    assert file.data == PNG and file.mime_type == "image/png"
    assert FakeClient.started[0].invocations[-1] == ("readFiles", {"paths": ["a.png"]})

    result = interpreter.read_files(ReadFilesAction(type="readFiles", paths=["a.png"]))
    assert result["status"] == "success"
    text = result["content"][0]["text"]
    assert "use publish_file" in text and str(PNG) not in text and "image/png" in text
