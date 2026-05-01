#!/usr/bin/env python3
"""GamerAI CLI client — submits a prompt and prints the result.

Usage:
    python client/client.py "Explain GPUs simply"
    python client/client.py --workers
    python client/client.py --earnings
    python client/client.py --metrics
    python client/client.py --result <job_id>

Env:
    COORDINATOR_URL (default http://localhost:8000)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = os.getenv("COORDINATOR_URL", "http://localhost:8000")


def http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def submit_and_wait(url: str, prompt: str, timeout: float, poll: float) -> int:
    out = http("POST", f"{url}/generate", {"prompt": prompt})
    job_id = out["job_id"]
    print(f"job_id: {job_id}", file=sys.stderr)
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = http("GET", f"{url}/result/{job_id}")
        status = res.get("status")
        if status == "complete":
            print(res.get("text", ""))
            print(
                f"\n[worker={res.get('worker_id')} "
                f"tokens={res.get('completion_tokens')} "
                f"earnings=${res.get('earnings')} "
                f"latency={res.get('duration_seconds')}s]",
                file=sys.stderr,
            )
            return 0
        if status == "error":
            print(f"error: {res.get('error')}", file=sys.stderr)
            return 1
        time.sleep(poll)
    print("timeout waiting for result", file=sys.stderr)
    return 2


def main() -> int:
    p = argparse.ArgumentParser(description="GamerAI CLI client")
    p.add_argument("prompt", nargs="*", help="prompt to submit")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--result", metavar="JOB_ID", help="fetch a specific job result")
    p.add_argument("--workers", action="store_true")
    p.add_argument("--earnings", action="store_true")
    p.add_argument("--metrics", action="store_true")
    args = p.parse_args()

    try:
        if args.result:
            print(json.dumps(http("GET", f"{args.url}/result/{args.result}"), indent=2))
            return 0
        if args.workers:
            print(json.dumps(http("GET", f"{args.url}/workers"), indent=2))
            return 0
        if args.earnings:
            print(json.dumps(http("GET", f"{args.url}/earnings"), indent=2))
            return 0
        if args.metrics:
            print(json.dumps(http("GET", f"{args.url}/metrics"), indent=2))
            return 0
        if not args.prompt:
            p.error("provide a prompt or one of --result/--workers/--earnings/--metrics")
        prompt = " ".join(args.prompt)
        return submit_and_wait(args.url, prompt, args.timeout, args.poll)
    except urllib.error.URLError as e:
        print(f"connection error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
