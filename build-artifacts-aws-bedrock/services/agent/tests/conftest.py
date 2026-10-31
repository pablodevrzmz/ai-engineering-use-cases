"""Shared fixtures: fake AWS credentials for moto, a moto S3 bucket, and a sandbox stand-in.

No test here talks to AWS. moto answers S3, the sandbox is faked at the method level publish.py depends on, and the
model is replaced by a scripted agent in test_events.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `import app` works however pytest is launched

for name, value in {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
}.items():
    os.environ.setdefault(name, value)

from app.sandbox import SandboxError, SandboxFile  # noqa: E402 - after the path and env setup above

BUCKET = "files-test"


class FakeSandbox:
    """The two reads publish_file makes, over an in-memory dict of path -> (bytes, mime)."""

    def __init__(self, files: dict[str, tuple[bytes, str | None]] | None = None) -> None:
        self.files = dict(files or {})
        self.calls: list[tuple[str, str]] = []

    def file_size(self, path: str) -> int:
        self.calls.append(("file_size", path))
        if path not in self.files:
            raise SandboxError(f"'{path}' is not a file in the sandbox: No such file or directory")
        return len(self.files[path][0])

    def read_bytes(self, path: str) -> SandboxFile:
        self.calls.append(("read_bytes", path))
        data, mime = self.files[path]
        return SandboxFile(path=path, data=data, mime_type=mime)


@pytest.fixture
def s3():
    """A moto S3 client with the files bucket created."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def fake_sandbox() -> FakeSandbox:
    return FakeSandbox(
        {
            "report.csv": (b"name,color,price\napple,red,1.2\n", "text/csv"),
            "out/chart.png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),
            "empty.txt": (b"", "text/plain"),
            "big.bin": (b"x" * 2048, None),
        }
    )
