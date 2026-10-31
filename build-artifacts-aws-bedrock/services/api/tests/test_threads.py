from datetime import datetime, timedelta, timezone

import pytest

from app.threads import THREAD_ID, ThreadNotFound, Threads, new_thread_id, runtime_session_id
from tests.conftest import ALICE, BOB


class Clock:
    """A controllable clock: every call moves time forward by ``step``."""

    def __init__(self, start=datetime(2026, 1, 1, tzinfo=timezone.utc), step=timedelta(seconds=1)):
        self.now = start
        self.step = step

    def __call__(self):
        current = self.now
        self.now += self.step
        return current


def test_ids_have_the_documented_shape():
    thread_id = new_thread_id(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert thread_id.startswith("20260102030405-")
    assert THREAD_ID.match(thread_id)
    session = runtime_session_id(thread_id, ALICE)
    assert session.startswith(thread_id + "-") and len(session) >= 33
    assert session != runtime_session_id(thread_id, BOB)  # same thread id, different user, different session


def test_create_then_get(ddb_table):
    threads = Threads(ddb_table, ttl_days=30, clock=Clock())
    item = threads.create(ALICE, "  Fruits  ")
    assert item["title"] == "Fruits"
    assert item["created_at"] == "2026-01-01T00:00:00Z"
    assert item["expires_at"] == int((datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)).timestamp())
    assert threads.get_owned(ALICE, item["thread_id"])["runtime_session_id"] == item["runtime_session_id"]
    assert threads.create(ALICE)["title"] == "New thread"


def test_list_is_newest_first_and_honours_limit(ddb_table):
    threads = Threads(ddb_table, ttl_days=30, clock=Clock())
    ids = [threads.create(ALICE, f"t{i}")["thread_id"] for i in range(3)]
    threads.create(BOB, "not alice's")
    assert [t["thread_id"] for t in threads.list_for(ALICE)] == list(reversed(ids))
    assert [t["title"] for t in threads.list_for(ALICE, limit=2)] == ["t2", "t1"]


def test_missing_foreign_and_malformed_look_the_same(ddb_table):
    threads = Threads(ddb_table, ttl_days=30)
    thread_id = threads.create(ALICE)["thread_id"]
    for user, tid in [(BOB, thread_id), (ALICE, new_thread_id()), (ALICE, "../etc/passwd"), (ALICE, "")]:
        with pytest.raises(ThreadNotFound):
            threads.get_owned(user, tid)


def test_expired_rows_are_hidden_before_ttl_removes_them(ddb_table):
    clock = Clock(step=timedelta(days=31))  # every call jumps past the TTL
    threads = Threads(ddb_table, ttl_days=30, clock=clock)
    thread_id = threads.create(ALICE)["thread_id"]
    assert threads.list_for(ALICE) == []
    with pytest.raises(ThreadNotFound):
        threads.get_owned(ALICE, thread_id)


def test_touch_bumps_updated_at_and_ignores_missing_rows(ddb_table):
    threads = Threads(ddb_table, ttl_days=30, clock=Clock(step=timedelta(minutes=5)))
    thread_id = threads.create(ALICE)["thread_id"]
    threads.touch(ALICE, thread_id)
    item = threads.get_owned(ALICE, thread_id)
    assert item["updated_at"] > item["created_at"]
    threads.touch(ALICE, new_thread_id())  # no row: must not raise or create one
    assert len(threads.list_for(ALICE)) == 1


# ---- through the API --------------------------------------------------------


def test_create_list_get_via_api(client, auth, other_auth):
    created = client.post("/v1/threads", json={"title": "Fruits"}, headers=auth)
    assert created.status_code == 201
    thread = created.json()
    assert set(thread) == {"threadId", "title", "createdAt", "updatedAt", "expiresAt"}
    assert "runtime_session_id" not in created.text

    listed = client.get("/v1/threads", headers=auth).json()["threads"]
    assert [t["threadId"] for t in listed] == [thread["threadId"]]
    assert client.get("/v1/threads", headers=other_auth).json() == {"threads": []}
    assert client.get("/v1/threads?limit=101", headers=auth).status_code == 422

    assert client.get(f"/v1/threads/{thread['threadId']}", headers=auth).json() == thread
    foreign = client.get(f"/v1/threads/{thread['threadId']}", headers=other_auth)
    missing = client.get(f"/v1/threads/{new_thread_id()}", headers=auth)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"error": "not_found"}


def test_delete_removes_files_then_row(client, auth, other_auth, s3):
    thread_id = client.post("/v1/threads", json={}, headers=auth).json()["threadId"]
    keep_id = client.post("/v1/threads", json={}, headers=auth).json()["threadId"]
    file_id = "0" * 32
    s3.put_object(Bucket="files-test", Key=f"users/{ALICE}/threads/{thread_id}/{file_id}/a.csv", Body=b"a")
    s3.put_object(Bucket="files-test", Key=f"users/{ALICE}/threads/{thread_id}/{file_id}/b.csv", Body=b"b")
    s3.put_object(Bucket="files-test", Key=f"users/{ALICE}/threads/{keep_id}/{file_id}/keep.csv", Body=b"k")

    assert client.delete(f"/v1/threads/{thread_id}", headers=other_auth).status_code == 404
    assert client.delete(f"/v1/threads/{thread_id}", headers=auth).status_code == 204
    assert client.get(f"/v1/threads/{thread_id}", headers=auth).status_code == 404
    assert client.delete(f"/v1/threads/{thread_id}", headers=auth).status_code == 404

    remaining = [o["Key"] for o in s3.list_objects_v2(Bucket="files-test", Prefix=f"users/{ALICE}/").get("Contents", [])]
    assert remaining == [f"users/{ALICE}/threads/{keep_id}/{file_id}/keep.csv"]
