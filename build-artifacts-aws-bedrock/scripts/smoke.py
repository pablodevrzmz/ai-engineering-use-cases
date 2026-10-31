#!/usr/bin/env python3
"""End-to-end smoke test for build-artifacts-aws-bedrock.

Signs in as the demo user, creates a thread, asks the agent for a small CSV, reads the SSE stream, downloads the
published file through the presigned URL and deletes the thread. Exit code 0 means the whole path works.

Usage (the Makefile fills these from Terraform outputs):
    python3 scripts/smoke.py --url http://<alb> --region us-east-1 --client-id <id> --email demo@example.com \
        [--password ... | DEMO_USER_PASSWORD=... | --tfvars infra/platform/terraform.tfvars]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import ssl

import boto3
from botocore import UNSIGNED
from botocore.config import Config

try:  # python.org builds on macOS ship without system CA certificates; certifi fixes the presigned https download
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    SSL_CONTEXT = ssl.create_default_context()

DEFAULT_PROMPT = (
    "Create a CSV file named fruits.csv with 3 rows (name, color, price) and publish it with the title Fruits."
)


def read_password(args: argparse.Namespace) -> str:
    """Password precedence: --password, DEMO_USER_PASSWORD, then the tfvars file (never printed)."""
    if args.password:
        return args.password
    if os.environ.get("DEMO_USER_PASSWORD"):
        return os.environ["DEMO_USER_PASSWORD"]
    if args.tfvars and os.path.exists(args.tfvars):
        text = open(args.tfvars, encoding="utf-8").read()
        match = re.search(r'^\s*demo_user_password\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    sys.exit("no password: pass --password, set DEMO_USER_PASSWORD or point --tfvars at terraform.tfvars")


def sign_in(region: str, client_id: str, email: str, password: str) -> str:
    """USER_PASSWORD_AUTH against Cognito with an unsigned client (the app client has no secret)."""
    idp = boto3.client("cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED))
    result = idp.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    return result["AuthenticationResult"]["IdToken"]


def call(url: str, token: str | None, method: str, path: str, body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream" if path.endswith("/runs") else "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


def iter_sse(response):
    """Yield parsed `data:` events from an SSE response; comment lines (heartbeats) are skipped."""
    buffer: list[str] = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if buffer:
                yield json.loads("\n".join(buffer))
                buffer = []
            continue
        if line.startswith(":"):
            print(".", end="", flush=True)
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].strip())
    if buffer:
        yield json.loads("\n".join(buffer))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="API base URL (Terraform output api_url)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--client-id", required=True, help="Cognito app client id")
    parser.add_argument("--email", required=True, help="demo user email")
    parser.add_argument("--password")
    parser.add_argument("--tfvars", help="terraform.tfvars to read demo_user_password from")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=int, default=420, help="seconds to wait for the run (cold start included)")
    args = parser.parse_args()

    started = time.time()
    password = read_password(args)

    with call(args.url, None, "GET", "/health") as resp:
        assert resp.status == 200, f"/health returned {resp.status}"
    print("health ok")

    token = sign_in(args.region, args.client_id, args.email, password)
    print("signed in")

    with call(args.url, token, "POST", "/v1/threads", {"title": "smoke"}) as resp:
        thread = json.load(resp)
    thread_id = thread["threadId"]
    print(f"thread {thread_id}")

    counts: dict[str, int] = {}
    file_event = None
    error = None
    text = []
    print("run: ", end="", flush=True)
    with call(args.url, token, "POST", f"/v1/threads/{thread_id}/runs", {"prompt": args.prompt}, timeout=args.timeout) as resp:
        for event in iter_sse(resp):
            kind = event.get("type", "?")
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "text":
                text.append(event.get("delta", ""))
            elif kind == "tool":
                print(f"[{event.get('name')}:{event.get('status')}]", end="", flush=True)
            elif kind == "file":
                file_event = event
                print(f"[file {event.get('name')} {event.get('size')} B]", end="", flush=True)
            elif kind == "error":
                error = event
    print()
    print(f"events: {counts}")
    if text:
        print("assistant:", "".join(text).strip()[:300])
    if error:
        print("RESULT: fail (agent error)", json.dumps(error))
        return 2
    if not file_event:
        print("RESULT: fail (no file event)")
        return 3
    if counts.get("done", 0) != 1:
        print("RESULT: fail (no done event)")
        return 4

    with call(args.url, token, "GET", f"/v1/threads/{thread_id}/files/{file_event['fileId']}") as resp:
        described = json.load(resp)
    url = described["url"]
    with urllib.request.urlopen(url, timeout=60, context=SSL_CONTEXT) as download:
        body = download.read()
        disposition = download.headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment"), f"unexpected Content-Disposition: {disposition!r}"
    assert len(body) == file_event["size"], f"downloaded {len(body)} bytes, event said {file_event['size']}"
    print(f"downloaded {file_event['name']} ({len(body)} bytes) as attachment; first line: {body.splitlines()[0][:80]!r}")

    with call(args.url, token, "GET", f"/v1/threads/{thread_id}/files") as resp:
        listed = json.load(resp)["files"]
    assert any(f["fileId"] == file_event["fileId"] for f in listed), "file missing from the thread listing"

    with call(args.url, token, "DELETE", f"/v1/threads/{thread_id}") as resp:
        assert resp.status == 204, f"delete returned {resp.status}"
    try:
        call(args.url, token, "GET", f"/v1/threads/{thread_id}").close()
        print("RESULT: fail (thread still readable after delete)")
        return 5
    except urllib.error.HTTPError as exc:
        assert exc.code == 404, f"expected 404 after delete, got {exc.code}"

    print(f"RESULT: ok ({time.time() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
