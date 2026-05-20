"""GamerAI CLI chat — hits the live coordinator the same way the
browser does, so what you see here matches what the web UI sees.

Why this exists: when the browser UI surfaces a weird response (echoed
history, hallucinated User: turns, etc.), we need a way to reproduce
that against the exact same /generate + /result endpoints from a
terminal so we can iterate on the fix without a browser in the loop.

Auth: pass `--token gai_...` or set `GAMERAI_TOKEN` in the env. On the
prod VPS the admin token lives at `/opt/gamerai/.env.prod`
(API_TOKEN=...); for a member token use the value the user saw on
their /invite/<code> redemption page.

Usage:
    python tools/cchat.py                      # new conversation, prompt loop
    python tools/cchat.py --message "hi"       # single prompt, exit
    python tools/cchat.py --conversation <id>  # resume an existing conversation
    python tools/cchat.py --list               # list your conversations
    python tools/cchat.py --base http://localhost:8000  # point elsewhere

The interactive loop prints each assistant turn as it streams in (poll
every 200ms against /result/<job_id>, same cadence as the browser),
then accepts the next prompt. Ctrl-C to exit; `:exit`, `:new`, `:list`,
`:history`, `:id` slash commands also work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import httpx


DEFAULT_BASE = os.environ.get("GAMERAI_BASE", "https://ai.dallinlayton.com")
POLL_INTERVAL_SECONDS = 0.2
SUBMIT_TIMEOUT_SECONDS = 15.0
GENERATE_OVERALL_TIMEOUT_SECONDS = 300.0


def _die(msg: str, code: int = 1) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(code)


def _bearer_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def list_conversations(base: str, token: str) -> list[dict]:
    with httpx.Client(base_url=base, headers=_bearer_header(token), timeout=10) as c:
        r = c.get("/conversations")
        r.raise_for_status()
        return (r.json() or {}).get("conversations", []) or []


def create_conversation(base: str, token: str, title: Optional[str]) -> str:
    body: dict = {}
    if title:
        body["title"] = title
    with httpx.Client(base_url=base, headers=_bearer_header(token), timeout=10) as c:
        r = c.post("/conversations", json=body)
        r.raise_for_status()
        return r.json()["conversation_id"]


def fetch_conversation(base: str, token: str, cid: str) -> dict:
    with httpx.Client(base_url=base, headers=_bearer_header(token), timeout=10) as c:
        r = c.get(f"/conversations/{cid}")
        r.raise_for_status()
        return r.json()


def submit_prompt(base: str, token: str, cid: str, prompt: str) -> dict:
    """POST /generate against the coordinator. Mirrors the BFF proxy's
    payload exactly so the server-side codepath is identical."""
    body = {"prompt": prompt, "conversation_id": cid}
    with httpx.Client(
        base_url=base,
        headers={**_bearer_header(token), "Content-Type": "application/json"},
        timeout=SUBMIT_TIMEOUT_SECONDS,
    ) as c:
        r = c.post("/generate", json=body)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail") or ""
            except ValueError:
                detail = r.text
            _die(f"submit failed: HTTP {r.status_code} {detail}", code=2)
        return r.json()


def stream_result(base: str, token: str, job_id: str) -> dict:
    """Same polling pattern the browser JS uses: hit /result/<job_id>
    every 200ms, render the latest text in-place, stop when done."""
    deadline = time.time() + GENERATE_OVERALL_TIMEOUT_SECONDS
    last_text = ""
    final: dict = {}
    with httpx.Client(
        base_url=base, headers=_bearer_header(token), timeout=10,
    ) as c:
        while time.time() < deadline:
            try:
                r = c.get(f"/result/{job_id}")
                if r.status_code == 404:
                    # Edge case: result not yet visible — keep trying.
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                r.raise_for_status()
                payload = r.json()
            except (httpx.HTTPError, ValueError):
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            text = payload.get("text") or ""
            if text != last_text:
                # Re-print the assistant turn from scratch each tick.
                # ANSI \r + clear-to-EOL would be cleaner but breaks in
                # logs, dumb terminals, and pipes. The simple approach
                # below stays readable when output is redirected to a
                # file and lets you see partial-token corrections in
                # real time during dev.
                delta = text[len(last_text):]
                sys.stdout.write(delta)
                sys.stdout.flush()
                last_text = text
            status = payload.get("status")
            if payload.get("done") or status in ("complete", "error"):
                final = payload
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            _die(
                f"timed out after {GENERATE_OVERALL_TIMEOUT_SECONDS:.0f}s",
                code=3,
            )
    sys.stdout.write("\n")
    sys.stdout.flush()
    return final


def print_history(messages: list[dict]) -> None:
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").rstrip()
        status = m.get("status") or "complete"
        suffix = f"  [{status}]" if status != "complete" else ""
        print(f"--- {role}{suffix} ---")
        print(text)
        print()


def banner(cid: str, base: str) -> None:
    print(f"GamerAI cchat — base={base} conversation={cid}")
    print("commands: :exit  :new  :list  :history  :id")
    print()


def interactive_loop(base: str, token: str, cid: str) -> None:
    banner(cid, base)
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line == ":exit":
            return
        if line == ":new":
            cid = create_conversation(base, token, title=None)
            banner(cid, base)
            continue
        if line == ":list":
            convs = list_conversations(base, token)
            for c in convs[:20]:
                marker = "*" if c["conversation_id"] == cid else " "
                title = (c.get("title") or "(untitled)").replace("\n", " ")[:60]
                print(f" {marker} {c['conversation_id']}  {title}")
            print()
            continue
        if line == ":history":
            data = fetch_conversation(base, token, cid)
            print_history(data.get("messages", []) or [])
            continue
        if line == ":id":
            print(cid)
            continue
        sub = submit_prompt(base, token, cid, line)
        job_id = sub["job_id"]
        sys.stdout.write("ai> ")
        sys.stdout.flush()
        result = stream_result(base, token, job_id)
        if result.get("status") == "error":
            err = result.get("error") or result.get("text") or "(no error message)"
            print(f"[error: {err}]")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GamerAI CLI chat")
    p.add_argument("--base", default=DEFAULT_BASE,
                   help=f"coordinator base URL (default: {DEFAULT_BASE})")
    p.add_argument("--token", default=os.environ.get("GAMERAI_TOKEN"),
                   help="bearer token (or set GAMERAI_TOKEN env var)")
    p.add_argument("--conversation", default=None,
                   help="resume an existing conversation by id")
    p.add_argument("--title", default=None,
                   help="title for a freshly-created conversation")
    p.add_argument("--message", "-m", default=None,
                   help="send a single message and exit (non-interactive)")
    p.add_argument("--list", action="store_true",
                   help="list your conversations and exit")
    p.add_argument("--history", action="store_true",
                   help="print the conversation history and exit "
                        "(requires --conversation)")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.token:
        _die(
            "no bearer token — pass --token or set GAMERAI_TOKEN. "
            "On prod the admin token is in /opt/gamerai/.env.prod (API_TOKEN=).",
        )

    base = args.base.rstrip("/")

    if args.list:
        for c in list_conversations(base, args.token):
            title = (c.get("title") or "(untitled)").replace("\n", " ")[:60]
            print(f"{c['conversation_id']}  {title}")
        return 0

    if args.history:
        if not args.conversation:
            _die("--history requires --conversation <id>")
        data = fetch_conversation(base, args.token, args.conversation)
        print_history(data.get("messages", []) or [])
        return 0

    cid = args.conversation or create_conversation(base, args.token, args.title)

    if args.message:
        sub = submit_prompt(base, args.token, cid, args.message)
        sys.stdout.write("ai> ")
        sys.stdout.flush()
        result = stream_result(base, args.token, sub["job_id"])
        if result.get("status") == "error":
            err = result.get("error") or result.get("text") or "(no error message)"
            print(f"[error: {err}]")
            return 1
        print(f"# conversation_id={cid}")
        return 0

    interactive_loop(base, args.token, cid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
