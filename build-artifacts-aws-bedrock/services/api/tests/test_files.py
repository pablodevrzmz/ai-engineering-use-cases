import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.files import FileNotFound, Files, content_disposition, safe_content_type, thread_prefix
from app.threads import iso_utc, new_thread_id, utc_now
from tests.conftest import ALICE, BOB, BUCKET

THREAD = new_thread_id()


def put(s3, name, *, user=ALICE, thread=THREAD, body=b"x,y\n1,2\n", content_type="text/csv", metadata=None):
    """Store an object the way the agent's ``publish_file`` tool does."""
    file_id = secrets.token_hex(16)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{thread_prefix(user, thread)}{file_id}/{name}",
        Body=body,
        ContentType=content_type,
        Metadata=metadata or {},
    )
    return file_id


def meta(*, created=None, expires=None, title=None):
    out = {}
    if created is not None:
        out["created-at"] = iso_utc(created)
    if expires is not None:
        out["expires-at"] = iso_utc(expires)
    if title is not None:
        out["title"] = title
    return out


@pytest.fixture
def files(s3):
    return Files(s3, BUCKET, ttl_days=30, url_ttl_seconds=300)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("text/csv", "text/csv"),
        ("Text/CSV; charset=utf-8", "text/csv"),
        ("application/pdf", "application/pdf"),
        ("text/html", "application/octet-stream"),
        ("application/xhtml+xml", "application/octet-stream"),
        ("image/svg+xml", "application/octet-stream"),
        ("application/atom+xml", "application/octet-stream"),
        ("text/javascript", "application/octet-stream"),
        ("application/javascript", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
def test_safe_content_type(given, expected):
    assert safe_content_type(given) == expected


def test_content_disposition_has_ascii_fallback_and_utf8_form():
    assert content_disposition("fruits.csv") == "attachment; filename=\"fruits.csv\"; filename*=UTF-8''fruits.csv"
    assert content_disposition('résumé "v2".csv') == (
        "attachment; filename=\"r_sum_ _v2_.csv\"; filename*=UTF-8''r%C3%A9sum%C3%A9%20%22v2%22.csv"
    )


def test_list_hides_expired_by_metadata_and_sorts_oldest_first(s3, files):
    now = utc_now()
    put(s3, "newer.csv", metadata=meta(created=now - timedelta(hours=1), expires=now + timedelta(days=29)))
    put(s3, "older.csv", metadata=meta(created=now - timedelta(days=2), expires=now + timedelta(days=28), title="Older"))
    put(s3, "gone.csv", metadata=meta(created=now - timedelta(days=31), expires=now - timedelta(days=1)))
    put(s3, "plain.csv")  # no metadata: falls back to LastModified + 30 days
    put(s3, "bobs.csv", user=BOB)
    s3.put_object(Bucket=BUCKET, Key=f"{thread_prefix(ALICE, THREAD)}not-a-file-id/x.csv", Body=b"junk")

    listed = files.list_thread(ALICE, THREAD)
    assert [f.name for f in listed] == ["older.csv", "newer.csv", "plain.csv"]
    older = listed[0].to_api()
    assert older["title"] == "Older" and older["size"] == 8 and older["mimeType"] == "text/csv"
    assert older["threadId"] == THREAD and older["expiresAt"].endswith("Z")
    assert listed[2].title == "plain.csv"  # title defaults to the name


def test_list_hides_expired_by_last_modified_fallback(s3):
    now = utc_now()
    put(s3, "plain.csv")  # expiry = LastModified + ttl; with ttl 0 that is already past
    put(s3, "tagged.csv", metadata=meta(expires=now + timedelta(days=1)))
    zero_ttl = Files(s3, BUCKET, ttl_days=0, url_ttl_seconds=300)
    assert [f.name for f in zero_ttl.list_thread(ALICE, THREAD)] == ["tagged.csv"]


def test_describe_404s_before_and_after_s3(s3, files):
    expired = put(s3, "gone.csv", metadata=meta(expires=utc_now() - timedelta(seconds=1)))
    live = put(s3, "live.csv")
    assert files.describe(ALICE, THREAD, live).name == "live.csv"
    for file_id in ["abc", "0" * 31, "0" * 33, "G" * 32, live.upper(), "../" + live, secrets.token_hex(16), expired]:
        with pytest.raises(FileNotFound):
            files.describe(ALICE, THREAD, file_id)
    with pytest.raises(FileNotFound):
        files.describe(BOB, THREAD, live)  # someone else's thread


def test_download_url_is_short_lived_and_never_renders(s3, files):
    file_id = put(s3, "report.html", body=b"<script>alert(1)</script>", content_type="text/html")
    info = files.describe(ALICE, THREAD, file_id)
    assert info.mime_type == "text/html"  # what was stored...

    url, expires_at = files.download_url(info)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc.startswith(f"{BUCKET}.s3")  # virtual-hosted style
    assert "X-Amz-Expires=300" in url
    assert query["response-content-disposition"][0].startswith("attachment; filename=\"report.html\"")
    assert query["response-content-type"] == ["application/octet-stream"]  # ...but never served as HTML
    assert query["response-cache-control"] == ["private, no-store"]
    assert timedelta(seconds=290) < expires_at - utc_now() <= timedelta(seconds=300)


def test_delete_thread_only_touches_its_prefix(s3, files):
    other_thread = new_thread_id()
    for _ in range(3):
        put(s3, "a.csv")
    put(s3, "keep.csv", thread=other_thread)
    assert files.delete_thread(ALICE, THREAD) == 3
    assert files.list_thread(ALICE, THREAD) == []
    assert [f.name for f in files.list_thread(ALICE, other_thread)] == ["keep.csv"]


# ---- through the API --------------------------------------------------------


def test_file_routes(client, auth, other_auth, s3):
    thread_id = client.post("/v1/threads", json={}, headers=auth).json()["threadId"]
    file_id = put(s3, "fruits.csv", thread=thread_id, metadata=meta(title="Fruits"))

    listed = client.get(f"/v1/threads/{thread_id}/files", headers=auth)
    assert listed.status_code == 200
    assert [f["fileId"] for f in listed.json()["files"]] == [file_id]
    assert set(listed.json()["files"][0]) == {"fileId", "name", "title", "size", "mimeType", "threadId", "createdAt", "expiresAt"}

    described = client.get(f"/v1/threads/{thread_id}/files/{file_id}", headers=auth)
    assert described.status_code == 200
    assert described.headers["cache-control"] == "no-store"
    body = described.json()
    assert body["file"]["title"] == "Fruits" and body["url"].startswith("https://")
    assert datetime.fromisoformat(body["urlExpiresAt"].replace("Z", "+00:00")) > datetime.now(timezone.utc)

    assert client.get(f"/v1/threads/{thread_id}/files/{file_id}", headers=other_auth).status_code == 404
    assert client.get(f"/v1/threads/{thread_id}/files/not-hex", headers=auth).status_code == 404
    assert client.get(f"/v1/threads/{thread_id}/files/{secrets.token_hex(16)}", headers=auth).json() == {"error": "not_found"}
