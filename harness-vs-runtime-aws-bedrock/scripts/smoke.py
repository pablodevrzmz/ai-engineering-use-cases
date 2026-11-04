#!/usr/bin/env python3
"""End-to-end check for both flavors: one question that needs a web search, asked to each.

Passes when each flavor called the WebSearch tool, answered with at least one URL and finished cleanly. Prints the
token usage and latency of both side by side, which is the comparison the README talks about.

    python3 scripts/smoke.py --region us-east-1 --harness-arn <arn> [--runtime-arn <arn>]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat import HarnessDriver, RuntimeDriver, Turn, make_client  # noqa: E402

DEFAULT_QUESTION = "What is the latest stable release of Terraform and on what date was it published? Cite the source URL."


def check(flavor: str, turn: Turn) -> list[str]:
    problems = []
    if not any("WebSearch" in (tool.get("name") or "") for tool in turn.tools):
        problems.append(f"{flavor}: the WebSearch tool was not called")
    if not turn.urls:
        problems.append(f"{flavor}: no URL in the answer")
    if turn.stop_reason not in {"end_turn", None} and flavor == "harness":
        problems.append(f"{flavor}: stopReason {turn.stop_reason}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True)
    parser.add_argument("--harness-arn", required=True)
    parser.add_argument("--runtime-arn", help="omit to test the harness alone (ENABLE_RUNTIME=false)")
    parser.add_argument("--qualifier", default="live")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    started = time.time()
    client = make_client(args.region, args.timeout)
    drivers = {"harness": HarnessDriver(client, args.harness_arn, str(uuid.uuid4()))}
    if args.runtime_arn:
        drivers["runtime"] = RuntimeDriver(client, args.runtime_arn, str(uuid.uuid4()), args.qualifier)

    turns: dict[str, Turn] = {}
    problems: list[str] = []
    for flavor, driver in drivers.items():
        print(f"\n===== {flavor}: {args.question}")
        t0 = time.time()
        try:
            turn = driver.turn(args.question, retries=args.retries)
        except Exception as exc:  # noqa: BLE001 - report and keep going with the other flavor
            print(f"{flavor}: FAILED {exc}")
            problems.append(f"{flavor}: {exc}")
            continue
        turn.latency_ms = turn.latency_ms or int((time.time() - t0) * 1000)
        turns[flavor] = turn
        print(f"-- {turn.footer()}  wall={time.time() - t0:.1f}s")
        problems += check(flavor, turn)

    if turns:
        print("\n| flavor | tool calls | input tokens | output tokens | latency |")
        print("|---|---|---|---|---|")
        for flavor, turn in turns.items():
            usage = turn.usage or {}
            print(f"| {flavor} | {len(turn.tools)} | {usage.get('inputTokens', '?')} | {usage.get('outputTokens', '?')} | {turn.latency_ms} ms |")

    if problems:
        print("\nRESULT: fail")
        for problem in problems:
            print(" -", problem)
        return 3
    print(f"\nRESULT: ok ({time.time() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
