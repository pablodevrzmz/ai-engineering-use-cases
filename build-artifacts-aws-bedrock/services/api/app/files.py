"""Files the agent published for a thread, stored in S3.

The agent (running in AgentCore) writes each published file to
``users/{user_id}/threads/{thread_id}/{file_id}/{name}`` with a little object
metadata. This module only *reads* that prefix: lists a thread's files, hides
the expired ones, and hands the browser a short-lived presigned GET so the
bytes go straight from S3 without passing through the API container.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config

from .threads import iso_utc, utc_now

FILE_ID = re.compile(r"^[0-9a-f]{32}$")
OCTET_STREAM = "application/octet-stream"
# Types a browser would *render* if served from the bucket's origin. Forcing
# them to octet-stream turns a published page into a plain download, so no
# script can ever run under the bucket's domain.
UNSAFE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"}
MAX_LISTED = 200


class FileNotFound(LookupError):
    """Malformed id, missing object, expired object or another user's file."""


def thread_prefix(user_id: str, thread_id: str) -> str:
    return f"users/{user_id}/threads/{thread_id}/"


def safe_content_type(mime: str | None) -> str:
    """The content type S3 is told to send back; never a renderable one."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    if not base:
        return OCTET_STREAM
    if base in UNSAFE_TYPES or base.endswith("+xml") or base.endswith("/javascript"):
        return OCTET_STREAM
    return base


def content_disposition(name: str) -> str:
    """``attachment`` with both an ASCII fallback and the RFC 5987 UTF-8 form.

    Older or stricter clients read ``filename``; modern browsers prefer
    ``filename*`` and so keep non-ASCII names intact.
    """
    # Printable ASCII (space included) is allowed inside the quoted string;
    # the quote and backslash would end or escape it, so they become "_".
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def make_s3_client(region: str) -> Any:
    """An S3 client whose presigned URLs work for any bucket in any region.

    SigV4 is the only signature newer regions accept, and virtual-hosted
    addressing (``bucket.s3.region.amazonaws.com``) is the form S3 wants for
    presigned URLs today.
    """
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class FileInfo:
    """One published file, as described by its S3 key and HEAD response."""

    key: str
    file_id: str
    name: str
    title: str
    size: int
    mime_type: str
    thread_id: str
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_head(cls, key: str, thread_id: str, file_id: str, name: str, head: dict, ttl_days: int) -> "FileInfo":
        """Build from ``head_object``; fall back to object timestamps when metadata is absent.

        ``expires-at`` metadata is what the agent *intended*; the S3 lifecycle
        rule works from ``LastModified`` + ``ttl_days``. Reading the metadata
        first keeps the API honest with the agent's event, and the fallback
        keeps objects uploaded by other tools visible with a sane expiry.
        """
        metadata = {k.lower(): v for k, v in (head.get("Metadata") or {}).items()}
        last_modified: datetime = head["LastModified"]
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)
        created = _parse_iso(metadata.get("created-at")) or last_modified
        expires = _parse_iso(metadata.get("expires-at")) or (last_modified + timedelta(days=ttl_days))
        return cls(
            key=key,
            file_id=file_id,
            name=name,
            title=metadata.get("title") or name,
            size=int(head.get("ContentLength", 0)),
            mime_type=head.get("ContentType") or OCTET_STREAM,
            thread_id=thread_id,
            created_at=created,
            expires_at=expires,
        )

    def expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def to_api(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "name": self.name,
            "title": self.title,
            "size": self.size,
            "mimeType": self.mime_type,
            "threadId": self.thread_id,
            "createdAt": iso_utc(self.created_at),
            "expiresAt": iso_utc(self.expires_at),
        }


class Files:
    """Read-side of the files prefix for one bucket."""

    def __init__(self, s3: Any, bucket: str, ttl_days: int, url_ttl_seconds: int) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._ttl_days = ttl_days
        self._url_ttl = url_ttl_seconds

    def list_thread(self, user_id: str, thread_id: str) -> list[FileInfo]:
        """Live files of a thread, oldest first, at most ``MAX_LISTED``."""
        prefix = thread_prefix(user_id, thread_id)
        now = utc_now()
        found: list[FileInfo] = []
        for key in self._keys(prefix, limit=MAX_LISTED):
            info = self._describe_key(key, prefix, thread_id)
            if info is not None and not info.expired(now):
                found.append(info)
        found.sort(key=lambda f: (f.created_at, f.name))
        return found

    def describe(self, user_id: str, thread_id: str, file_id: str) -> FileInfo:
        """One live file of the caller's thread, or ``FileNotFound``."""
        # Validate before touching S3: a bad id can never be a hit, and the
        # regex check is free compared with a round trip.
        if not FILE_ID.match(file_id):
            raise FileNotFound(file_id)
        prefix = thread_prefix(user_id, thread_id)
        keys = self._keys(f"{prefix}{file_id}/", limit=1)
        if not keys:
            raise FileNotFound(file_id)
        info = self._describe_key(keys[0], prefix, thread_id)
        if info is None or info.expired(utc_now()):
            raise FileNotFound(file_id)
        return info

    def download_url(self, info: FileInfo) -> tuple[str, datetime]:
        """A presigned GET for the browser, plus the moment it stops working.

        The ``Response*`` parameters are baked into the signature, so whatever
        the object was uploaded with, S3 answers *this* link as an attachment
        with a non-renderable type and no caching.
        """
        url = self._s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": info.key,
                "ResponseContentDisposition": content_disposition(info.name),
                "ResponseContentType": safe_content_type(info.mime_type),
                "ResponseCacheControl": "private, no-store",
            },
            ExpiresIn=self._url_ttl,
        )
        return url, utc_now() + timedelta(seconds=self._url_ttl)

    def delete_thread(self, user_id: str, thread_id: str) -> int:
        """Remove every object under the thread's prefix; returns the count."""
        prefix = thread_prefix(user_id, thread_id)
        deleted = 0
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:  # delete_objects takes up to 1000 keys, one page's worth
                self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
        return deleted

    def _keys(self, prefix: str, limit: int) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
                if len(keys) >= limit:
                    return keys
        return keys

    def _describe_key(self, key: str, prefix: str, thread_id: str) -> FileInfo | None:
        file_id, _, name = key[len(prefix):].partition("/")
        if not FILE_ID.match(file_id) or not name or "/" in name:
            return None  # not something the agent wrote; ignore rather than fail the listing
        head = self._s3.head_object(Bucket=self._bucket, Key=key)
        return FileInfo.from_head(key, thread_id, file_id, name, head, self._ttl_days)
