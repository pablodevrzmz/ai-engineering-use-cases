"""publish.py: name and type sanitising, and the publish_file tool against moto S3."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.publish import MAX_FILES_PER_RUN, FilePublisher, content_type_for, human_size, safe_file_name
from tests.conftest import BUCKET, FakeSandbox

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("report.csv", "report.csv"),
        ("out/sales 2026 (final).xlsx", "sales_2026__final_.xlsx"),
        ("../../etc/passwd", "passwd"),
        (".hidden", "hidden"),
        ("..", "file"),
        ("", "file"),
        ("dir/", "dir"),
        ("C:\\work\\notes.txt", "notes.txt"),
        ("caf\u00e9.txt", "caf_.txt"),
    ],
)
def test_safe_file_name(given: str, expected: str) -> None:
    assert safe_file_name(given) == expected


def test_safe_file_name_caps_length() -> None:
    name = safe_file_name("a" * 300 + ".csv")
    assert len(name) == 120
    assert name == "a" * 120


@pytest.mark.parametrize(
    ("name", "sandbox_mime", "data", "expected"),
    [
        ("data.csv", None, b"", "text/csv"),
        ("page.html", None, b"<html>", "application/octet-stream"),
        ("page.xhtml", None, b"", "application/octet-stream"),
        ("chart.svg", "image/svg+xml", b"", "application/octet-stream"),
        ("feed.xml", None, b"", "application/octet-stream"),
        ("app.js", None, b"", "application/octet-stream"),
        ("image", None, PNG, "image/png"),
        ("doc", None, b"%PDF-1.7", "application/pdf"),
        ("photo", None, b"\xff\xd8\xff\xe0", "image/jpeg"),
        ("bundle", None, b"PK\x03\x04", "application/zip"),
        ("noext", "application/pdf; charset=binary", b"", "application/pdf"),
        ("noext", "text/html", b"", "application/octet-stream"),
        ("noext", None, b"\x00\x01", "application/octet-stream"),
        ("report.pdf", "text/plain", b"", "application/pdf"),
    ],
)
def test_content_type_for(name: str, sandbox_mime: str | None, data: bytes, expected: str) -> None:
    assert content_type_for(name, sandbox_mime, data) == expected


def make_publisher(s3, sandbox: FakeSandbox, events: list, *, max_bytes: int = 1024, ttl_days: int = 30) -> FilePublisher:
    publisher = FilePublisher(
        sandbox=sandbox,
        s3=s3,
        bucket=BUCKET,
        user_id="user-1",
        thread_id="thread-1",
        ttl_days=ttl_days,
        max_bytes=max_bytes,
        emit=events.append,
    )
    publisher.begin_run("run-1")
    return publisher


def test_publish_file_stores_object_and_emits_record(s3, fake_sandbox: FakeSandbox) -> None:
    events: list[dict] = []
    publisher = make_publisher(s3, fake_sandbox, events)

    result = json.loads(publisher.publish_file("report.csv", "Fruits"))

    record = result["published"]
    assert "note" in result and "download card" in result["note"]
    assert len(record["fileId"]) == 32 and int(record["fileId"], 16) >= 0
    assert record["name"] == "report.csv"
    assert record["title"] == "Fruits"
    assert record["size"] == len(fake_sandbox.files["report.csv"][0])
    assert record["mimeType"] == "text/csv"
    assert record["threadId"] == "thread-1"
    assert record["runId"] == "run-1"
    assert record["createdAt"].endswith("Z") and record["expiresAt"].endswith("Z")
    created = datetime.fromisoformat(record["createdAt"])
    expires = datetime.fromisoformat(record["expiresAt"])
    assert (expires - created).days == 30

    assert events == [{"type": "file", **record}]

    key = f"users/user-1/threads/thread-1/{record['fileId']}/report.csv"
    head = s3.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentType"] == "text/csv"
    assert head["ContentDisposition"] == 'attachment; filename="report.csv"'
    assert head["Metadata"] == {
        "created-at": record["createdAt"],
        "expires-at": record["expiresAt"],
        "run-id": "run-1",
        "title": "Fruits",
    }
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == fake_sandbox.files["report.csv"][0]
    # The size is checked before the transfer, so an oversized file is refused without pulling it through.
    assert fake_sandbox.calls == [("file_size", "report.csv"), ("read_bytes", "report.csv")]


def test_publish_file_keeps_binary_type_and_sanitises_title(s3, fake_sandbox: FakeSandbox) -> None:
    events: list[dict] = []
    publisher = make_publisher(s3, fake_sandbox, events)

    record = json.loads(publisher.publish_file("out/chart.png", "Ventas \u2014 Q3 \u00e9"))["published"]

    assert record["name"] == "chart.png"
    assert record["mimeType"] == "image/png"
    assert record["title"] == "Ventas ? Q3 ?"  # S3 metadata is ASCII only
    head = s3.head_object(Bucket=BUCKET, Key=f"users/user-1/threads/thread-1/{record['fileId']}/chart.png")
    assert head["Metadata"]["title"] == "Ventas ? Q3 ?"


def test_publish_file_title_defaults_to_name(s3, fake_sandbox: FakeSandbox) -> None:
    publisher = make_publisher(s3, fake_sandbox, [])
    record = json.loads(publisher.publish_file("report.csv"))["published"]
    assert record["title"] == "report.csv"


def test_publish_file_refuses_empty_file(s3, fake_sandbox: FakeSandbox) -> None:
    events: list[dict] = []
    publisher = make_publisher(s3, fake_sandbox, events)
    result = json.loads(publisher.publish_file("empty.txt"))
    assert "empty" in result["error"]
    assert events == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_publish_file_refuses_oversized_file(s3, fake_sandbox: FakeSandbox) -> None:
    events: list[dict] = []
    publisher = make_publisher(s3, fake_sandbox, events, max_bytes=1024)
    result = json.loads(publisher.publish_file("big.bin"))
    assert "'big.bin' is 2,048 bytes; the limit is 1,024 bytes" in result["error"]
    assert ("read_bytes", "big.bin") not in fake_sandbox.calls
    assert events == []


def test_human_size_reports_the_production_cap_in_mb() -> None:
    assert human_size(15 * 1024 * 1024) == "15 MB"
    assert human_size(int(1.5 * 1024 * 1024)) == "1.5 MB"
    assert human_size(2048) == "2,048 bytes"


def test_publish_file_reports_missing_path_as_error_result(s3, fake_sandbox: FakeSandbox) -> None:
    publisher = make_publisher(s3, fake_sandbox, [])
    result = json.loads(publisher.publish_file("nope.csv"))
    assert "nope.csv" in result["error"] and "listFiles" in result["error"]


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "x" * 1025])
def test_publish_file_validates_path(s3, fake_sandbox: FakeSandbox, bad: str) -> None:
    publisher = make_publisher(s3, fake_sandbox, [])
    result = json.loads(publisher.publish_file(bad))
    assert "error" in result
    assert fake_sandbox.calls == []


def test_publish_file_caps_publishes_per_run(s3, fake_sandbox: FakeSandbox) -> None:
    events: list[dict] = []
    publisher = make_publisher(s3, fake_sandbox, events)
    for _ in range(MAX_FILES_PER_RUN):
        assert "published" in json.loads(publisher.publish_file("report.csv"))
    refused = json.loads(publisher.publish_file("report.csv"))
    assert "error" in refused and str(MAX_FILES_PER_RUN) in refused["error"]
    assert len(events) == MAX_FILES_PER_RUN

    publisher.begin_run("run-2")  # a new turn starts the count over and stamps the new run id
    record = json.loads(publisher.publish_file("report.csv"))["published"]
    assert record["runId"] == "run-2"


def test_publish_file_turns_storage_failures_into_error_results(fake_sandbox: FakeSandbox) -> None:
    class BrokenS3:
        def put_object(self, **kwargs):
            raise ConnectionError("s3 is down")

    events: list[dict] = []
    publisher = FilePublisher(
        sandbox=fake_sandbox, s3=BrokenS3(), bucket=BUCKET, user_id="u", thread_id="t", ttl_days=1, max_bytes=1024, emit=events.append
    )
    publisher.begin_run("r")
    result = json.loads(publisher.publish_file("report.csv"))
    assert "error" in result and "ConnectionError" in result["error"]
    assert events == []


def test_publish_file_is_a_strands_tool_without_self() -> None:
    publisher = FilePublisher(sandbox=FakeSandbox(), s3=None, bucket=BUCKET, user_id="u", thread_id="t", ttl_days=1, max_bytes=1, emit=print)
    spec = publisher.publish_file.tool_spec
    assert publisher.publish_file.tool_name == "publish_file"
    schema = spec["inputSchema"]["json"]
    assert set(schema["properties"]) == {"path", "title"}
    assert schema["required"] == ["path"]
    assert "download" in spec["description"]
