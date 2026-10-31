"""Conversation threads in DynamoDB.

A thread is the unit the browser lists and the agent's session maps to. The
table is tiny on purpose: hash key ``user_id`` (so one query returns a user's
threads and nobody else's), range key ``thread_id``, a ``runtime_session_id``
for AgentCore, timestamps and a TTL. Transcripts are not stored: the agent
keeps the conversation in its AgentCore session for the demo, and the files it
publishes live in S3 (see ``files.py``).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from boto3.dynamodb.conditions import Key

# ``%Y%m%d%H%M%S`` + "-" + 16 hex chars. Starting the id with a UTC stamp makes
# the range key sort chronologically, so "newest first" is a reverse query
# with no extra index.
THREAD_ID = re.compile(r"^\d{14}-[0-9a-f]{16}$")
DEFAULT_TITLE = "New thread"
MAX_TITLE_CHARS = 200


class ThreadNotFound(LookupError):
    """Raised for a thread that does not exist *or* belongs to someone else.

    One exception for both cases so the API answers the same 404 and never
    confirms that a guessed id exists.
    """


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """ISO 8601 with a ``Z`` suffix, the form JavaScript's ``Date`` parses."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_thread_id(now: datetime | None = None) -> str:
    stamp = (now or utc_now()).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{secrets.token_hex(8)}"


def runtime_session_id(thread_id: str, user_id: str) -> str:
    """The AgentCore session id for a thread.

    AgentCore requires at least 33 characters and isolates state per session
    id. Appending a hash of the user id means a thread id leaked or guessed by
    another user still maps to a different session, and the agent can check
    that the session belongs to the user named in the request header.
    """
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"{thread_id}-{digest}"


class Threads:
    """Thread persistence over a boto3 DynamoDB ``Table`` resource."""

    def __init__(self, table: Any, ttl_days: int, clock: Callable[[], datetime] = utc_now) -> None:
        self._table = table
        self._ttl = timedelta(days=ttl_days)
        self._clock = clock  # injectable so tests control ordering and expiry

    def create(self, user_id: str, title: str | None = None) -> dict[str, Any]:
        now = self._clock()
        thread_id = new_thread_id(now)
        item = {
            "user_id": user_id,
            "thread_id": thread_id,
            "runtime_session_id": runtime_session_id(thread_id, user_id),
            "title": (title or "").strip()[:MAX_TITLE_CHARS] or DEFAULT_TITLE,
            "created_at": iso_utc(now),
            "updated_at": iso_utc(now),
            # DynamoDB TTL wants epoch seconds as a number; the row disappears
            # on its own some time after this moment (typically within 48 h).
            "expires_at": int((now + self._ttl).timestamp()),
        }
        self._table.put_item(Item=item)
        return item

    def get_owned(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """Fetch a thread, raising ``ThreadNotFound`` unless it is live and the caller's."""
        if not THREAD_ID.match(thread_id):
            raise ThreadNotFound(thread_id)
        item = self._table.get_item(Key={"user_id": user_id, "thread_id": thread_id}).get("Item")
        if item is None or self._expired(item):
            raise ThreadNotFound(thread_id)
        return item

    def list_for(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """The caller's threads, newest first (reverse scan of the range key)."""
        result = self._table.query(
            KeyConditionExpression=Key("user_id").eq(user_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [item for item in result.get("Items", []) if not self._expired(item)]

    def touch(self, user_id: str, thread_id: str) -> None:
        """Bump ``updated_at`` after a run; a no-op if the row is gone."""
        try:
            self._table.update_item(
                Key={"user_id": user_id, "thread_id": thread_id},
                UpdateExpression="SET updated_at = :now",
                ConditionExpression="attribute_exists(thread_id)",
                ExpressionAttributeValues={":now": iso_utc(self._clock())},
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    def delete(self, user_id: str, thread_id: str) -> None:
        self._table.delete_item(Key={"user_id": user_id, "thread_id": thread_id})

    def _expired(self, item: dict[str, Any]) -> bool:
        # TTL deletion lags; hide rows that are past their expiry but still present.
        return int(item.get("expires_at", 0)) <= int(self._clock().timestamp())

    @staticmethod
    def to_api(item: dict[str, Any]) -> dict[str, Any]:
        """The JSON shape the browser sees (camelCase, no session id)."""
        expires = datetime.fromtimestamp(int(item["expires_at"]), tz=timezone.utc)
        return {
            "threadId": item["thread_id"],
            "title": item["title"],
            "createdAt": item["created_at"],
            "updatedAt": item["updated_at"],
            "expiresAt": iso_utc(expires),
        }
