#!/usr/bin/env python3
"""Talk to the research agent in either flavor from your terminal.

    python3 scripts/chat.py --flavor harness --arn <harness arn> --region us-east-1
    python3 scripts/chat.py --flavor runtime --arn <runtime arn> --region us-east-1 [--qualifier live]
    ... --once "What is the latest Terraform release?"      # one question, then exit

Both flavors are reached with the same boto3 client ("bedrock-agentcore") and SigV4 from your AWS profile:

* harness  -> InvokeHarness(harnessArn, runtimeSessionId, messages). The response is AgentCore's own event stream
              (messageStart, contentBlockStart/Delta/Stop, messageStop, metadata).
* runtime  -> InvokeAgentRuntime(agentRuntimeArn, qualifier, runtimeSessionId, payload). The response is whatever
              our container streams: here the SSE events services/agent emits (text, tool, done, error).

One session id is reused for the whole chat, so both flavors keep the conversation across turns (one microVM per
session on both). The harness additionally takes --actor-id, which scopes its managed long-term memory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EventStreamError

URL = re.compile(r"https?://[^\s<>()\[\]\"']+")

# Errors worth retrying right after `terraform apply`: IAM propagation (AccessDenied), the resource not yet visible
# (ResourceNotFound), a busy microVM (409 / ServiceUnavailable) and throttling.
RETRYABLE = {
    "AccessDeniedException",
    "ResourceNotFoundException",
    "ThrottlingException",
    "ServiceUnavailableException",
    "RuntimeClientError",
    "ConflictException",
    "ServiceException",
}


def extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for url in URL.findall(text):
        url = url.rstrip(".,;:")
        if url not in seen:
            seen.append(url)
    return seen


@dataclass
class Turn:
    """What one user turn produced, in the same shape for both flavors."""

    text: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, int] | None = None
    latency_ms: int | None = None
    error: str | None = None

    @property
    def urls(self) -> list[str]:
        return extract_urls(self.text)

    def footer(self) -> str:
        usage = self.usage or {}
        parts = [f"stop={self.stop_reason}"]
        if usage:
            parts.append(f"tokens in={usage.get('inputTokens')} out={usage.get('outputTokens')} total={usage.get('totalTokens')}")
        if self.latency_ms is not None:
            parts.append(f"latency={self.latency_ms} ms")
        parts.append(f"tool calls={len(self.tools)}")
        return "  ".join(parts)


STREAM_RETRYABLE = ("modelStreamErrorException", "ThrottlingException", "ServiceUnavailableException")


def with_retries(call: Callable[[], Any], *, retries: int, log: Callable[[str], None]) -> Any:
    """Run `call`, retrying the transient failures above with a doubling back-off (0.5 s, capped at 10 s).

    Two kinds of failure are retried: a ClientError on the request itself (IAM propagation, throttling, a busy
    microVM) and an EventStreamError raised while reading the harness stream when the model itself failed mid-turn
    (Bedrock's modelStreamErrorException, which Nova raises now and then on a malformed tool call).
    """
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            return call()
        except EventStreamError as exc:  # a ClientError subclass: must be checked first
            if not any(name in str(exc) for name in STREAM_RETRYABLE) or attempt == retries:
                raise RuntimeError(f"stream error: {exc}") from exc
            log(f"[model stream error: retrying in {delay:.1f}s ({attempt + 1}/{retries})]")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in RETRYABLE or attempt == retries:
                raise
            log(f"[{code}: retrying in {delay:.1f}s ({attempt + 1}/{retries})]")
        time.sleep(delay)
        delay = min(delay * 2, 10)
    raise AssertionError("unreachable")


def make_client(region: str, timeout: int):
    # No botocore retries: a retried POST would run the turn twice on the agent.
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(read_timeout=timeout, connect_timeout=10, retries={"max_attempts": 0}),
    )


class HarnessDriver:
    """InvokeHarness and its event stream."""

    def __init__(self, client, arn: str, session_id: str, actor_id: str | None = None, raw: bool = False) -> None:
        self.client, self.arn, self.session_id, self.actor_id, self.raw = client, arn, session_id, actor_id, raw

    def _invoke(self, prompt: str):
        kwargs: dict[str, Any] = {
            "harnessArn": self.arn,
            "runtimeSessionId": self.session_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
        }
        if self.actor_id:
            kwargs["actorId"] = self.actor_id
        return self.client.invoke_harness(**kwargs)

    def turn(self, prompt: str, *, retries: int, out=sys.stdout) -> Turn:
        return with_retries(lambda: self._turn(prompt, out), retries=retries, log=lambda m: print(m, file=sys.stderr))

    def _turn(self, prompt: str, out) -> Turn:
        turn = Turn()
        response = self._invoke(prompt)
        tool_inputs: dict[int, dict[str, Any]] = {}  # contentBlockIndex -> {"name", "toolUseId", "json": partial}
        for event in response["stream"]:
            if self.raw:
                print(json.dumps(event, default=str), file=sys.stderr)
            if "contentBlockStart" in event:
                block = event["contentBlockStart"]
                tool_use = block["start"].get("toolUse")
                if tool_use:
                    tool_inputs[block["contentBlockIndex"]] = {"name": tool_use.get("name"), "toolUseId": tool_use.get("toolUseId"), "json": ""}
            elif "contentBlockDelta" in event:
                block = event["contentBlockDelta"]
                delta = block["delta"]
                if delta.get("text"):
                    turn.text += delta["text"]
                    out.write(delta["text"])
                    out.flush()
                elif delta.get("toolUse") and block["contentBlockIndex"] in tool_inputs:
                    tool_inputs[block["contentBlockIndex"]]["json"] += delta["toolUse"].get("input", "") or ""
            elif "contentBlockStop" in event:
                index = event["contentBlockStop"]["contentBlockIndex"]
                if index in tool_inputs:
                    entry = tool_inputs.pop(index)
                    try:
                        entry_input = json.loads(entry["json"]) if entry["json"] else {}
                    except json.JSONDecodeError:
                        entry_input = {"raw": entry["json"]}
                    turn.tools.append({"name": entry["name"], "input": entry_input})
                    out.write(f"\n[tool {entry['name']} {json.dumps(entry_input)}]\n")
                    out.flush()
            elif "messageStop" in event:
                turn.stop_reason = event["messageStop"].get("stopReason")
            elif "metadata" in event:
                meta = event["metadata"]
                turn.usage = {k: v for k, v in meta.get("usage", {}).items() if isinstance(v, int)}
                turn.latency_ms = meta.get("metrics", {}).get("latencyMs")
            elif "hookEvent" in event:
                continue
            else:
                for key in ("internalServerException", "validationException", "runtimeClientError"):
                    if key in event:
                        raise RuntimeError(f"{key}: {event[key].get('message')}")
        out.write("\n")
        return turn


class RuntimeDriver:
    """InvokeAgentRuntime against our container, reading the SSE events it emits."""

    def __init__(self, client, arn: str, session_id: str, qualifier: str = "live", raw: bool = False) -> None:
        self.client, self.arn, self.session_id, self.qualifier, self.raw = client, arn, session_id, qualifier, raw

    def _invoke(self, prompt: str):
        return self.client.invoke_agent_runtime(
            agentRuntimeArn=self.arn,
            qualifier=self.qualifier,
            runtimeSessionId=self.session_id,
            contentType="application/json",
            accept="text/event-stream",
            payload=json.dumps({"prompt": prompt}).encode(),
        )

    def turn(self, prompt: str, *, retries: int, out=sys.stdout) -> Turn:
        turn = Turn()
        started = time.time()
        response = with_retries(lambda: self._invoke(prompt), retries=retries, log=lambda m: print(m, file=sys.stderr))
        body = response["response"]
        if not response.get("contentType", "").startswith("text/event-stream"):
            raw = body.read().decode("utf-8", "replace")
            raise RuntimeError(f"unexpected response ({response.get('contentType')}): {raw[:500]}")
        for line in body.iter_lines():
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            if self.raw:
                print(json.dumps(event), file=sys.stderr)
            kind = event.get("type")
            if kind == "text":
                turn.text += event["delta"]
                out.write(event["delta"])
                out.flush()
            elif kind == "tool" and event.get("status") == "start":
                turn.tools.append({"name": event.get("name"), "input": event.get("input", {})})
                out.write(f"\n[tool {event.get('name')} {json.dumps(event.get('input', {}))}]\n")
                out.flush()
            elif kind == "done":
                turn.stop_reason = event.get("stopReason")
                turn.usage = event.get("usage")
            elif kind == "error":
                turn.error = f"{event.get('code')}: {event.get('message')}"
        turn.latency_ms = int((time.time() - started) * 1000)
        out.write("\n")
        if turn.error:
            raise RuntimeError(turn.error)
        return turn


def make_driver(args: argparse.Namespace):
    client = make_client(args.region, args.timeout)
    if args.flavor == "harness":
        return HarnessDriver(client, args.arn, args.session_id, args.actor_id, raw=args.raw)
    return RuntimeDriver(client, args.arn, args.session_id, args.qualifier, raw=args.raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flavor", choices=["harness", "runtime"], required=True)
    parser.add_argument("--arn", required=True, help="harness ARN (output harness_arn) or runtime ARN (output runtime_arn)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--qualifier", default="live", help="runtime endpoint name (runtime flavor)")
    parser.add_argument("--session-id", default=str(uuid.uuid4()), help="runtime session id, 33-100 chars; reuse it to continue a conversation")
    parser.add_argument("--actor-id", help="harness only: the end user the managed memory is scoped to")
    parser.add_argument("--once", metavar="QUESTION", help="ask one question and exit")
    parser.add_argument("--raw", action="store_true", help="dump every stream event to stderr")
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=600, help="read timeout per turn (cold start included)")
    args = parser.parse_args()
    if not 33 <= len(args.session_id) <= 100:
        parser.error("--session-id must be 33 to 100 characters (a UUID works)")

    driver = make_driver(args)
    print(f"{args.flavor} · session {args.session_id}", file=sys.stderr)

    if args.once:
        turn = driver.turn(args.once, retries=args.retries)
        print(turn.footer(), file=sys.stderr)
        return 0

    print("type a question, /quit to leave", file=sys.stderr)
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt in {"/quit", "/q", "exit"}:
            return 0
        turn = driver.turn(prompt, retries=args.retries)
        print(turn.footer(), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
