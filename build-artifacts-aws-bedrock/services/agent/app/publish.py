"""The publish_file tool: moves a file the model wrote in the sandbox to S3 and announces it in the stream.

Why a relay at all: the sandbox is its own microVM with no credentials and no route to the user. The only thing
that can see both the sandbox and the bucket is this runtime, so the tool reads the file out of the sandbox as
bytes (sandbox.py) and writes it to S3 under the user's and thread's prefix. The Session API later signs a short
lived GET for the same key; the agent never hands out URLs.

Why the user and thread come from the runtime, not from the model: a `FilePublisher` is bound to one runtime
session, whose user id and thread id were checked against the request headers in main.py. The model only supplies
a sandbox path and a title, so it cannot write into someone else's prefix however it is prompted.

Why the stream gets a `file` event: a published file is a UI element, not text. The tool emits the record into the
turn's event queue the moment the object exists, so the chat can draw the download card while the model is still
composing its one-sentence answer.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from strands import tool

from app.sandbox import SandboxError, SandboxFile

logger = logging.getLogger(__name__)

MAX_FILES_PER_RUN = 10
MAX_PATH_CHARS = 1024
MAX_NAME_CHARS = 120
MAX_METADATA_CHARS = 256  # S3 caps user metadata at 2 KB in total; four values at 256 stay well inside it

OCTET_STREAM = "application/octet-stream"

UNSAFE_TYPES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"})
"""Types a browser would *render* if it ever fetched the object inline. They are stored as octet-stream so the file
is always a download, even if a presigned URL leaks into an <img> or an <iframe>. The extension stays as written."""

MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"PK\x03\x04", "application/zip"),
)

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SandboxReader(Protocol):
    """What publish_file needs from the sandbox; ExpiringCodeInterpreter provides it, tests use a fake."""

    def file_size(self, path: str) -> int: ...

    def read_bytes(self, path: str) -> SandboxFile: ...


class PublishError(ValueError):
    """A refused or failed publish; the message is written for the model, which has to act on it."""


def safe_file_name(name: str) -> str:
    """The base name of a sandbox path, reduced to a safe charset.

    Only [A-Za-z0-9._-] survive, leading dots go (no hidden files, no ".."), the length is capped, and an empty
    result becomes "file". The result is used in the S3 key, in Content-Disposition and in the download card, so
    one conservative rule covers all three.
    """
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    cleaned = _NAME_UNSAFE.sub("_", base).lstrip(".")[:MAX_NAME_CHARS]
    return cleaned or "file"


def content_type_for(name: str, sandbox_mime: str | None, data: bytes) -> str:
    """Pick the Content-Type to store: by extension, else what the sandbox said, else a few magic numbers.

    The extension wins because it is what the user will see and open; the sandbox's guess is a fallback for
    extension-less files. Renderable types are then forced to octet-stream (see UNSAFE_TYPES).
    """
    guessed, _ = mimetypes.guess_type(name)
    sandbox = (sandbox_mime or "").partition(";")[0].strip().lower()
    chosen = guessed or sandbox or next((mime for magic, mime in MAGIC if data.startswith(magic)), OCTET_STREAM)
    chosen = chosen.lower()
    if chosen in UNSAFE_TYPES or chosen.endswith("+xml") or chosen.endswith("/javascript"):
        return OCTET_STREAM
    return chosen


def ascii_metadata(value: str, limit: int = MAX_METADATA_CHARS) -> str:
    """S3 user metadata must be US-ASCII: drop control characters, replace the rest, cut at `limit`."""
    return _CONTROL.sub("", value).encode("ascii", "replace").decode("ascii").strip()[:limit]


def human_size(size: int) -> str:
    """A size the model can repeat to the user: whole or fractional MB above 1 MiB, bytes with separators below."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):g} MB"
    return f"{size:,} bytes"


def iso_utc(moment: datetime) -> str:
    """ISO 8601 with a trailing Z, the form the web page's Date parser and the API agree on."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FilePublisher:
    """One per runtime session: holds the sandbox, the S3 client and the identity the files are stored under."""

    def __init__(
        self,
        *,
        sandbox: SandboxReader,
        s3: Any,
        bucket: str,
        user_id: str,
        thread_id: str,
        ttl_days: int,
        max_bytes: int,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self._sandbox = sandbox
        self._s3 = s3
        self._bucket = bucket
        self._user_id = user_id
        self._thread_id = thread_id
        self._ttl_days = ttl_days
        self._max_bytes = max_bytes
        self._emit = emit
        self._lock = threading.Lock()  # Strands runs tools in worker threads; two publishes may overlap
        self._run_id: str | None = None
        self._published_in_run = 0

    def begin_run(self, run_id: str) -> None:
        """Called by run_turn before each turn: files are stamped with the run and the per-run cap starts over."""
        with self._lock:
            self._run_id = run_id
            self._published_in_run = 0

    def _count(self) -> None:
        with self._lock:
            if self._published_in_run >= MAX_FILES_PER_RUN:
                raise PublishError(f"this turn already published {MAX_FILES_PER_RUN} files; combine the data into fewer files")
            self._published_in_run += 1

    @tool
    def publish_file(self, path: str, title: str | None = None) -> str:
        """Publish a file you created in the sandbox so the user can download it.

        Call it once per file, after the file exists. Do not paste the file's contents into the chat afterwards:
        the chat already shows a download card for it.

        Args:
            path: The sandbox path you wrote the file to, for example "report.csv" or "out/chart.png".
            title: A short human title for the download card, for example "Sales by region".
        """
        try:
            record = self._publish(path, title)
        except PublishError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # never let a storage or transport failure escape into the model loop
            logger.exception("publish_file failed for %r in thread %s", path, self._thread_id)
            return json.dumps({"error": f"the file could not be published right now ({exc.__class__.__name__}); try once more"})
        note = "The chat already shows the download card. Tell the user in one sentence what the file contains."
        return json.dumps({"published": record, "note": note})

    def _publish(self, path: str, title: str | None) -> dict[str, Any]:
        path = (path or "").strip()
        if not path or len(path) > MAX_PATH_CHARS or _CONTROL.search(path):
            raise PublishError("give the sandbox path of the file as you wrote it, for example report.csv")
        self._count()
        try:
            size = self._sandbox.file_size(path)
            if size == 0:
                raise PublishError(f"'{path}' is empty; write the file first, then publish it")
            if size > self._max_bytes:
                raise PublishError(
                    f"'{path}' is {human_size(size)}; the limit is {human_size(self._max_bytes)}. Make it smaller and publish again"
                )
            file = self._sandbox.read_bytes(path)
        except SandboxError as exc:
            raise PublishError(f"{exc}; check the path with the code interpreter's listFiles, or rebuild the file") from exc

        name = safe_file_name(path)
        content_type = content_type_for(name, file.mime_type, file.data)
        file_id = uuid.uuid4().hex
        created = datetime.now(UTC).replace(microsecond=0)
        expires = created + timedelta(days=self._ttl_days)
        label = ascii_metadata(title or "") or name
        key = f"users/{self._user_id}/threads/{self._thread_id}/{file_id}/{name}"
        metadata = {
            "created-at": iso_utc(created),
            "expires-at": iso_utc(expires),
            "run-id": ascii_metadata(self._run_id or ""),
            "title": label,
        }
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=file.data,
            ContentType=content_type,
            ContentDisposition=f'attachment; filename="{name}"',
            Metadata=metadata,
        )
        record = {
            "fileId": file_id,
            "name": name,
            "title": label,
            "size": len(file.data),
            "mimeType": content_type,
            "threadId": self._thread_id,
            "runId": self._run_id,
            "createdAt": metadata["created-at"],
            "expiresAt": metadata["expires-at"],
        }
        logger.info("published %s (%d bytes, %s) as %s for thread %s", name, record["size"], content_type, file_id, self._thread_id)
        self._emit({"type": "file", **record})
        return record
