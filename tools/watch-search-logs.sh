#!/usr/bin/env bash
#
# Tail coordinator search-rewrite logs from prod.
#
# Watch how each search submission flows through the rewrite
# pipeline: original prompt → classifier output → parsed decision →
# final DDG query. Useful during manual UI testing of search-mode
# follow-ups; without it you'd be debugging by SSHing into the VPS
# and dumping the SQLite jobs table by hand.
#
# Usage:
#   tools/watch-search-logs.sh                # tail forever
#   tools/watch-search-logs.sh --since 5m     # backfill last 5m too
#   tools/watch-search-logs.sh --filter all   # show every coordinator
#                                             # line, not just search
#
# Env overrides (defaults work on the dev machine that bootstrapped
# the VPS — see ~/.ssh/id_ed25519_gamerai):
#   GAMERAI_VPS_HOST   default: ai.dallinlayton.com
#   GAMERAI_VPS_KEY    default: ~/.ssh/id_ed25519_gamerai
#
# Stop with Ctrl-C. Container keeps logging regardless — this just
# closes our viewer.
#
# Privacy note: search log lines include the user's prompt and the
# classifier's rewrite output (truncated to ~200 chars). See
# docs/project-gaps.md "Coordinator logs contain raw user prompts"
# for the rotation / scrubbing plan.

set -euo pipefail

HOST="${GAMERAI_VPS_HOST:-ai.dallinlayton.com}"
KEY="${GAMERAI_VPS_KEY:-$HOME/.ssh/id_ed25519_gamerai}"

SINCE_ARG=""
FILTER_ALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      [[ -n "${2:-}" ]] || { echo "--since needs an argument like 5m or 1h" >&2; exit 2; }
      SINCE_ARG="--since=$2"
      shift 2
      ;;
    --filter)
      [[ -n "${2:-}" ]] || { echo "--filter needs an argument" >&2; exit 2; }
      case "$2" in
        all) FILTER_ALL=1 ;;
        search) FILTER_ALL=0 ;;
        *) echo "unknown --filter value: $2 (use search|all)" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed -n '/^#/p' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# stdbuf -oL forces line-buffering on greps + python so we see output
# in real time instead of in 4 KB chunks. Without it the viewer feels
# laggy during interactive testing.
if [[ $FILTER_ALL -eq 1 ]]; then
  FILTER_CMD="cat"
else
  FILTER_CMD="stdbuf -oL grep -E search_rewrite\\|search_was_skipped\\|search_disabled\\|search_auto_disabled"
fi

ssh -i "$KEY" "root@$HOST" \
  "docker logs $SINCE_ARG --follow gamerai-coordinator 2>&1" \
| eval "$FILTER_CMD" \
| stdbuf -oL python3 -u -c '
"""Pretty-print structured coordinator log lines.

Plain JSON one-per-line is correct but hard to scan; this collapses
the noise (level / service / logger / message tokens that never
change) and surfaces the fields you actually care about while
debugging the search-rewrite pipeline.
"""
import json
import sys

# Field order tuned for the search-rewrite pipeline. ts + event
# always come first; then the user-facing strings; then the
# bookkeeping IDs at the end.
KEYS_ORDER = (
    "ts", "event",
    "decision",
    "original_prompt",
    "rewrite_output",
    "parsed_value",
    "final_query",
    "rewrite_was_used",
    "history_chars",
    "search_status",
    "image_status",
    "search_job_id",
    "rewrite_job_id",
    "image_job_id",
    "job_id",
    "worker_id",
    "error",
    "message",
)
_HIDE = {"level", "service", "logger"}

for line in sys.stdin:
    line = line.rstrip("\n")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # Non-JSON line (e.g. uvicorn startup banner) — print verbatim.
        print(line)
        continue
    parts = []
    for k in KEYS_ORDER:
        v = obj.get(k)
        if v is None or v == "":
            continue
        if k == "ts":
            parts.append(f"\033[90m{v}\033[0m")
        elif k == "event":
            parts.append(f"\033[1;36m{v}\033[0m")
        elif k == "decision":
            color = {
                "query": "\033[32m",       # green
                "skip": "\033[33m",        # yellow
                "error": "\033[31m",       # red
            }.get(str(v), "")
            parts.append(f"{color}decision={v}\033[0m")
        else:
            parts.append(f"{k}={v!r}")
    # Tail any keys we didn'\''t pre-order (forwards compat with new
    # extras the coordinator might add later).
    extras = [
        f"{k}={obj[k]!r}"
        for k in obj
        if k not in KEYS_ORDER and k not in _HIDE
    ]
    if extras:
        parts.append(" ".join(extras))
    print(" | ".join(parts), flush=True)
'
