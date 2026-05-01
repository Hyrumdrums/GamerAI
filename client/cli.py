#!/usr/bin/env python3
"""Tiny CLI for poking the GamerAI coordinator.

Examples:
    python client/cli.py submit "Explain GPUs simply"
    python client/cli.py result <job_id>
    python client/cli.py wait "Write a haiku about Redis"
    python client/cli.py workers
    python client/cli.py earnings
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error

DEFAULT_URL = "http://localhost:8000"


def http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def cmd_submit(args):
    out = http("POST", f"{args.url}/generate", {"prompt": args.prompt})
    print(out["job_id"])


def cmd_result(args):
    out = http("GET", f"{args.url}/result/{args.job_id}")
    print(json.dumps(out, indent=2))


def cmd_wait(args):
    job_id = http("POST", f"{args.url}/generate", {"prompt": args.prompt})["job_id"]
    print(f"job_id: {job_id}", file=sys.stderr)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        out = http("GET", f"{args.url}/result/{job_id}")
        if out.get("status") in ("complete", "error"):
            print(json.dumps(out, indent=2))
            return
        time.sleep(1)
    print("timeout waiting for result", file=sys.stderr)
    sys.exit(1)


def cmd_workers(args):
    print(json.dumps(http("GET", f"{args.url}/workers"), indent=2))


def cmd_earnings(args):
    print(json.dumps(http("GET", f"{args.url}/earnings"), indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("result")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_result)

    s = sub.add_parser("wait")
    s.add_argument("prompt")
    s.add_argument("--timeout", type=int, default=120)
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("workers")
    s.set_defaults(func=cmd_workers)

    s = sub.add_parser("earnings")
    s.set_defaults(func=cmd_earnings)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
