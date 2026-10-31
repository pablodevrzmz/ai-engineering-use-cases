"""The code sandbox: AgentCore Code Interpreter through strands-agents-tools, with two gotchas handled.

Gotcha 1, the absolute session TTL. AWS terminates a code interpreter session `session_timeout_seconds` after it
*starts*, no matter how recently it was used. strands-agents-tools caches sessions by name and keeps returning the
cached one without checking its status, so after the deadline every call in that thread fails. This subclass
remembers each session's deadline and forgets the session shortly before it, so the next call starts a fresh
sandbox instead of hitting a dead one. Variables and files in the old sandbox are gone at that point; the model is
told to publish files right after writing them for exactly that reason.

Gotcha 2, binary files in the model's context. The library's readFiles turns the response into `str(result)`, which
for a PNG or XLSX means hundreds of kilobytes of bytes-repr in the conversation. `read_files` is overridden to
replace every binary blob with a short note, and `read_bytes` gives the real bytes to runtime code (publish.py),
never to the model.

Private members of strands-agents-tools 0.8.9 this module relies on (pinned in requirements.txt):
    AgentCoreCodeInterpreter._ensure_session(name) -> (name, error_or_None): finds or creates the session a call uses.
    AgentCoreCodeInterpreter._sessions: dict name -> SessionInfo(session_id, description, client).
    agent_core_code_interpreter._session_mapping: module-level dict name -> AWS session id, shared by instances.
    AgentCoreCodeInterpreter.default_session: the name used when an action names no session.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from strands_tools.code_interpreter import agent_core_code_interpreter as library
from strands_tools.code_interpreter.agent_core_code_interpreter import AgentCoreCodeInterpreter
from strands_tools.code_interpreter.models import InitSessionAction, ReadFilesAction

logger = logging.getLogger(__name__)

TTL_MARGIN_SECONDS = 30.0
"""Sessions are dropped this long before AWS would terminate them, so a call never starts on a dying sandbox."""


class SandboxError(RuntimeError):
    """A sandbox read that failed or was refused. The message is phrased for the model, which sees it as a tool error."""


@dataclass(frozen=True)
class SandboxFile:
    """One file read out of the sandbox."""

    path: str
    data: bytes
    mime_type: str | None


def first_text(result: Any) -> str:
    """The first text block in a tool result or sandbox result; "" when there is none."""
    if not isinstance(result, dict):
        return ""
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _blob_bytes(blob: Any) -> bytes:
    """A readFiles blob as bytes. boto3 hands binary event-stream fields over as bytes; a raw JSON view is base64."""
    if isinstance(blob, (bytes, bytearray, memoryview)):
        return bytes(blob)
    if isinstance(blob, str):
        return base64.b64decode(blob)
    raise SandboxError(f"unexpected blob type {type(blob).__name__}")


def parse_read_files(response: Any, path: str) -> SandboxFile:
    """Extract one file from a readFiles response.

    The response is an event stream; the interesting event carries `result` with `isError` and `content`. A file
    arrives as a `resource` content item: `resource.blob` for binary files, `resource.text` for text files, plus
    `resource.mimeType` (measured against aws.codeinterpreter.v1; the library's own code reads the same shape).
    """
    for event in response.get("stream") or []:
        result = event.get("result")
        if result is None:
            continue
        if result.get("isError"):
            raise SandboxError(f"'{path}' could not be read: {first_text(result) or 'the sandbox reported an error'}")
        for block in result.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "resource":
                continue
            resource = block.get("resource") or {}
            if resource.get("blob") is not None:
                data = _blob_bytes(resource["blob"])
            elif resource.get("text") is not None:
                data = str(resource["text"]).encode("utf-8")
            else:
                continue
            return SandboxFile(path=path, data=data, mime_type=resource.get("mimeType") or None)
        raise SandboxError(f"'{path}' is not a file in the sandbox: {first_text(result) or 'nothing came back for it'}")
    raise SandboxError(f"'{path}' could not be read: no result came back from the sandbox")


def binary_note(resource: dict[str, Any]) -> str:
    """What the model sees in place of a binary file's bytes."""
    try:
        size = len(_blob_bytes(resource.get("blob")))
    except SandboxError:
        size = 0
    return f"<binary file, {size:,} bytes; use publish_file to give it to the user>"


def without_blobs(content: list[Any]) -> list[Any]:
    """readFiles content with every binary blob swapped for a note, so no bytes reach the model's context."""
    cleaned: list[Any] = []
    for block in content:
        resource = block.get("resource") if isinstance(block, dict) else None
        if isinstance(resource, dict) and resource.get("blob") is not None:
            trimmed = {key: value for key, value in resource.items() if key != "blob"}
            block = {**block, "resource": {**trimmed, "text": binary_note(resource)}}
        cleaned.append(block)
    return cleaned


class ExpiringCodeInterpreter(AgentCoreCodeInterpreter):
    """The library's `code_interpreter` tool plus `file_size` and `read_bytes`, on sessions that expire on time.

    One instance per conversation thread: `session_name` is the thread's sandbox name, so the model's calls (which
    omit session_name) and the runtime's reads all land in the same sandbox.
    """

    def __init__(
        self,
        *,
        region: str,
        session_timeout_seconds: int,
        session_name: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(region=region, session_name=session_name, session_timeout_seconds=session_timeout_seconds)
        self._clock = clock
        # Strands may run a batch of tool calls concurrently and the library documents its session cache as not
        # thread-safe, so session lookup/creation is serialised here.
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}  # session name -> clock() value at which the session is dropped

    # -- session lifetime -------------------------------------------------------------------------------------------

    def init_session(self, action: InitSessionAction) -> dict[str, Any]:
        """Start a session and note when to stop trusting it."""
        result = super().init_session(action)
        if result.get("status") == "success":
            self._deadlines[action.session_name] = self._clock() + self.session_timeout_seconds - TTL_MARGIN_SECONDS
            logger.info("sandbox session %s started (TTL %ss)", action.session_name, self.session_timeout_seconds)
        return result

    def _forget(self, name: str) -> None:
        """Drop every cache entry for a session, so the next call creates a new one.

        Both caches must go: with the module-level mapping left in place, the library's `init_session` would refuse
        the name as "already in use", and `_ensure_session` would try to reconnect to the terminated session.
        """
        self._sessions.pop(name, None)
        library._session_mapping.pop(name, None)  # noqa: SLF001 - see the module docstring
        self._deadlines.pop(name, None)

    def _ensure_session(self, session_name: str | None) -> tuple[str, dict[str, Any] | None]:
        with self._lock:
            name = session_name or self.default_session
            deadline = self._deadlines.get(name)
            if name in self._sessions and deadline is not None and self._clock() >= deadline:
                logger.info("sandbox session %s reached its TTL; the next call starts a fresh one", name)
                self._forget(name)
            return super()._ensure_session(session_name)

    def _invoke(self, method: str, params: dict[str, Any], session_name: str | None = None) -> Any:
        """One data-plane call on the thread's session; a transport failure forgets the session (it may be gone)."""
        name, error = self._ensure_session(session_name)
        if error:
            raise SandboxError(f"the sandbox is unavailable: {first_text(error) or 'it could not be started'}")
        try:
            return self._sessions[name].client.invoke(method, params)
        except Exception as exc:
            self._forget(name)
            logger.warning("%s failed on sandbox session %s: %s", method, name, exc)
            raise SandboxError(f"the sandbox call failed: {exc}") from exc

    # -- reads for runtime code (not for the model) -----------------------------------------------------------------

    def file_size(self, path: str) -> int:
        """Size of a sandbox file in bytes, without transferring it.

        A one-line executeCode is far cheaper than reading the file, and it lets publish_file refuse oversized files
        before a multi-megabyte event stream is pulled through the runtime.
        """
        code = f"print(__import__('os').path.getsize({json.dumps(path)}))"
        response = self._invoke("executeCode", {"code": code, "language": "python", "clearContext": False})
        for event in response.get("stream") or []:
            result = event.get("result")
            if result is None:
                continue
            structured = result.get("structuredContent") or {}
            stdout = str(structured.get("stdout") or first_text(result) or "").strip()
            if not result.get("isError") and stdout.isdigit():
                return int(stdout)
            stderr_lines = str(structured.get("stderr") or "").strip().splitlines()
            reason = stderr_lines[-1] if stderr_lines else (first_text(result) or "nothing came back for it")
            raise SandboxError(f"'{path}' is not a file in the sandbox: {reason}")
        raise SandboxError(f"'{path}' could not be checked: no result came back from the sandbox")

    def read_bytes(self, path: str) -> SandboxFile:
        """A sandbox file as bytes, for the runtime to relay elsewhere."""
        response = self._invoke("readFiles", {"paths": [path]})
        return parse_read_files(response, path)

    # -- the model-facing readFiles ---------------------------------------------------------------------------------

    def read_files(self, action: ReadFilesAction) -> dict[str, Any]:
        """The library's readFiles with binary blobs replaced by a note; text files come through as before."""
        name, error = self._ensure_session(action.session_name)
        if error:
            return error
        response = self._sessions[name].client.invoke("readFiles", {"paths": action.paths})
        for event in response.get("stream") or []:
            result = event.get("result")
            if result is None:
                continue
            content = without_blobs(result.get("content") or [])
            return {"status": "error" if result.get("isError") else "success", "content": [{"text": str(content)}]}
        return {"status": "error", "content": [{"text": f"Failed to create tool result: {response!s}"}]}
