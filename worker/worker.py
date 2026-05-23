"""Worker: pulls jobs, simulates a gamer machine, posts results to coordinator."""
import datetime as dt
import json
import logging
import random
import socket
import time
import uuid
from typing import Optional

import httpx
import redis

from shared.auth import auth_headers
from shared.config import (
    AVAILABILITY_WINDOW,
    COLD_START_AFTER_IDLE,
    COLD_START_MAX,
    COLD_START_MIN,
    COORDINATOR_URL,
    HEARTBEAT_INTERVAL,
    JOB_QUEUE,
    MOCK_INFERENCE,
    MODEL,
    NETWORK_DELAY_MAX,
    NETWORK_DELAY_MIN,
    OLLAMA_URL,
    POLL_INTERVAL,
    WORKER_ID_OVERRIDE,
    job_queue_for,
    redis_kwargs,
)

WORKER_ID = WORKER_ID_OVERRIDE or f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"


# ---------- structured logging ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "service": "worker",
            "worker_id": WORKER_ID,
            "message": record.getMessage(),
        }
        for k in ("job_id", "event"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("worker")


# ---------- availability ----------
def _parse_window(spec: str) -> Optional[tuple[int, int]]:
    if not spec or spec.lower() in ("always", "any", ""):
        return None
    try:
        a, b = spec.split("-")
        return int(a) % 24, int(b) % 24
    except (ValueError, TypeError):
        return None


_WINDOW = _parse_window(AVAILABILITY_WINDOW)


def is_available(now: Optional[dt.datetime] = None) -> bool:
    if _WINDOW is None:
        return True
    start, end = _WINDOW
    h = (now or dt.datetime.utcnow()).hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight


# ---------- redis ----------
def connect_redis() -> redis.Redis:
    while True:
        try:
            client = redis.Redis(decode_responses=True, **redis_kwargs())
            client.ping()
            return client
        except redis.RedisError as e:
            log.warning("redis not ready: %s", e, extra={"event": "redis_retry"})
            time.sleep(2)


# ---------- coordinator HTTP ----------
def post(http: httpx.Client, path: str, body: dict, timeout: float = 5.0) -> Optional[dict]:
    try:
        resp = http.post(f"{COORDINATOR_URL}{path}", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("coordinator %s failed: %s", path, e)
        return None


def _search_available() -> bool:
    """Probe ddgs + trafilatura. Search is opt-in for the docker
    worker too — the wheels are listed in requirements.txt but a
    custom build may have stripped them, and we'd rather advertise
    chat-only than have a search job land here and explode."""
    try:
        import ddgs  # noqa: F401
        import trafilatura  # noqa: F401
        return True
    except ImportError:
        return False


WORKER_TOOLS: list[str] = ["chat"]
if _search_available():
    WORKER_TOOLS.append("search")


def register(http: httpx.Client) -> None:
    body = {
        "worker_id": WORKER_ID,
        # Advertise the tool set so the coordinator's _ensure_live_worker
        # check counts this worker against the right requested-tool 503
        # check, and so /jobs/next routes search jobs here when running
        # in the dockerized dev/test setup.
        "capabilities": {"tools": WORKER_TOOLS},
    }
    for attempt in range(20):
        if post(http, "/register", body) is not None:
            log.info(
                "registered tools=%s", WORKER_TOOLS,
                extra={"event": "registered"},
            )
            return
        time.sleep(2)
    log.error("failed to register; continuing")


def heartbeat(http: httpx.Client, status: str) -> None:
    post(http, "/heartbeat", {"worker_id": WORKER_ID, "status": status}, timeout=3)


# ---------- inference ----------
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


# Minimum wall-clock between /jobs/partial pushes for real Ollama
# streams. Tight enough that tokens feel live, loose enough that we
# don't hammer the coordinator with one POST per token. The mock
# branch overrides this and flushes per character so a dev watching
# the UI sees a visible type-in effect even when the underlying
# response is tiny.
PARTIAL_FLUSH_INTERVAL = 0.25

# A fixed lorem-ipsum-flavored body the mock streams back regardless
# of the input prompt. Deliberately NOT an echo of the prompt — that
# was confusing in multi-turn dev tests where the prepended chat
# history looked like the model was leaking it back. This is long
# enough that a 200ms client poll captures several visible chunks.
_MOCK_RESPONSE = (
    "Streaming demo response: lorem ipsum dolor sit amet, consectetur "
    "adipiscing elit. Sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua. Ut enim ad minim veniam, quis nostrud "
    "exercitation ullamco laboris nisi ut aliquip ex ea commodo "
    "consequat. Duis aute irure dolor in reprehenderit in voluptate "
    "velit esse cillum dolore eu fugiat nulla pariatur."
)
# Per-character wall-clock during the mock stream. 2.5ms ⇒ the full
# response above takes about a second — fast enough that it doesn't
# stall the demo, slow enough that the client poll loop sees multiple
# intermediate states and renders a real type-in effect.
_MOCK_CHAR_DELAY = 0.0025


def run_inference(
    prompt: str,
    model: Optional[str],
    http: httpx.Client,
    on_partial=None,
    messages: Optional[list[dict]] = None,
) -> dict:
    """Generate a response, streaming intermediate text via ``on_partial``
    (called with the FULL accumulated text, not a delta). The final
    return is the same shape as before so /jobs/complete callers don't
    need to change.

    When ``messages`` is provided the call is routed to Ollama's
    ``/api/chat`` endpoint, so the model's own chat template is applied
    instead of feeding it a hand-rolled ``User:/Assistant:`` transcript.
    Single-shot jobs (canaries, /generate without a conversation) keep
    the legacy ``/api/generate`` path."""
    if MOCK_INFERENCE:
        # Push every character — no throttle. The mock is a UX demo,
        # not a load test, and flushing per char makes the streaming
        # animation visible regardless of how the client paces polls.
        text = ""
        for ch in _MOCK_RESPONSE:
            text += ch
            time.sleep(_MOCK_CHAR_DELAY)
            if on_partial:
                on_partial(text)
        return {
            "text": text,
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(text),
            "model": "mock",
        }
    use_model = model or MODEL
    text = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    last_flush = 0.0
    if messages:
        endpoint = f"{OLLAMA_URL}/api/chat"
        payload = {"model": use_model, "messages": messages, "stream": True}
    else:
        endpoint = f"{OLLAMA_URL}/api/generate"
        payload = {"model": use_model, "prompt": prompt, "stream": True}
    with http.stream("POST", endpoint, json=payload, timeout=600) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            # /api/chat streams {"message": {"role": "assistant",
            # "content": "..."}, "done": ...}; /api/generate streams
            # {"response": "...", "done": ...}. Read whichever shape
            # the current chunk carries.
            token = (
                (chunk.get("message") or {}).get("content", "")
                if messages
                else chunk.get("response", "")
            )
            if token:
                text += token
            if chunk.get("done"):
                prompt_tokens = chunk.get("prompt_eval_count")
                completion_tokens = chunk.get("eval_count")
                break
            now = time.time()
            if on_partial and (now - last_flush) >= PARTIAL_FLUSH_INTERVAL:
                on_partial(text)
                last_flush = now
    if on_partial:
        on_partial(text)
    return {
        "text": text,
        "prompt_tokens": int(prompt_tokens or estimate_tokens(prompt)),
        "completion_tokens": int(completion_tokens or estimate_tokens(text)),
        "model": use_model,
    }


# ---------- search inference ----------
SEARCH_RESULT_COUNT = 5
SEARCH_PAGE_CHAR_CAP = 2500
SEARCH_FETCH_TIMEOUT = 6.0


# Backend rotation order. ddgs ships several free backends — each has
# its own rate-limit pool, so falling through on RatelimitException
# or empty results multiplies per-IP capacity. Same list as the
# windows-agent so dev/test and prod behave identically.
_SEARCH_BACKENDS = (
    "duckduckgo", "mojeek", "brave", "bing", "yahoo", "startpage",
)
# In-process TTL cache for duplicate queries on the same worker.
_SEARCH_CACHE_TTL = 600  # 10 min
_SEARCH_CACHE_MAX = 512
try:
    from cachetools import TTLCache
    _search_cache: "TTLCache | None" = TTLCache(
        maxsize=_SEARCH_CACHE_MAX, ttl=_SEARCH_CACHE_TTL,
    )
except ImportError:
    _search_cache = None  # Cache silently disabled.


def _ddg_search(query: str, max_results: int = SEARCH_RESULT_COUNT) -> list[dict]:
    cache_key = (query, max_results)
    if _search_cache is not None and cache_key in _search_cache:
        return list(_search_cache[cache_key])
    from ddgs import DDGS
    try:
        from ddgs.exceptions import RatelimitException
    except ImportError:
        RatelimitException = Exception
    last_err: Optional[Exception] = None
    for backend in _SEARCH_BACKENDS:
        try:
            with DDGS() as ddg:
                hits = ddg.text(
                    query, max_results=max_results, backend=backend,
                ) or []
            if hits:
                if _search_cache is not None:
                    _search_cache[cache_key] = list(hits)
                return hits
        except RatelimitException as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    return []


def _fetch_and_extract(url: str, http: httpx.Client) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        resp = http.get(
            url,
            timeout=SEARCH_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            },
        )
        resp.raise_for_status()
    except Exception:
        return ""
    try:
        return (trafilatura.extract(resp.text) or "").strip()
    except Exception:
        return ""


def _build_search_system(query: str, results: list[dict], mode: str) -> str:
    lines = [
        "You are a search assistant. The user has asked a question and the",
        "system has retrieved fresh web results below. Use them — and only",
        "them — to answer concisely and accurately.",
        "",
        "Rules:",
        "- Cite sources inline as [1], [2], etc., matching the numbers below.",
        "- If the results don't contain enough information, say so plainly.",
        "- Prefer the most specific source for a given claim.",
        "- Do not invent URLs or facts that aren't in the results.",
        "",
        f"User query: {query}",
        "",
        "Search results:",
    ]
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()
        url = (r.get("href") or r.get("url") or "").strip()
        snippet = (r.get("body") or "").strip()
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if snippet:
            lines.append(f"    Snippet: {snippet}")
        body = (r.get("_extracted") or "").strip() if mode == "comprehensive" else ""
        if body:
            lines.append(f"    Body: {body[:SEARCH_PAGE_CHAR_CAP]}")
        lines.append("")
    return "\n".join(lines)


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return url


def run_search_inference(
    prompt: str,
    job: dict,
    http: httpx.Client,
    on_partial=None,
) -> dict:
    """Search + summarize for the dockerized worker. Mirrors the
    windows-agent path so dev-mode and prod-mode behave identically:
    DDG → optional fetch+extract → Ollama summary with citation
    instructions. Raises on failure so the coordinator records error
    rather than fabricating an answer with no grounding."""
    mode = ((job.get("search") or {}).get("mode") or "fast").lower()
    if mode not in ("fast", "comprehensive"):
        mode = "fast"
    try:
        results = _ddg_search(prompt, max_results=SEARCH_RESULT_COUNT)
    except Exception as e:
        raise RuntimeError(f"web search failed: {e}") from e
    if not results:
        raise RuntimeError(
            "no search results — try a different query or uncheck search"
        )
    if mode == "comprehensive":
        for r in results:
            url = r.get("href") or r.get("url") or ""
            if url:
                r["_extracted"] = _fetch_and_extract(url, http)
    system_text = _build_search_system(prompt, results, mode)
    base = list(job.get("messages") or [])
    if not base:
        base = [{"role": "user", "content": prompt}]
    chat_messages = [{"role": "system", "content": system_text}] + base
    summary = run_inference(
        prompt,
        job.get("model"),
        http,
        on_partial=on_partial,
        messages=chat_messages,
    )
    sources = [
        {
            "n": i,
            "title": (r.get("title") or "").strip()
                     or _domain_of(r.get("href") or ""),
            "url": (r.get("href") or r.get("url") or "").strip(),
            "domain": _domain_of(r.get("href") or r.get("url") or ""),
        }
        for i, r in enumerate(results, start=1)
    ]
    return {
        "text": summary["text"],
        "prompt_tokens": summary["prompt_tokens"],
        "completion_tokens": summary["completion_tokens"],
        "model": summary["model"],
        "sources": sources,
    }


# ---------- gamer-machine realism ----------
def simulate_network_delay() -> None:
    delay = random.uniform(NETWORK_DELAY_MIN, NETWORK_DELAY_MAX)
    if delay > 0:
        time.sleep(delay)


def maybe_simulate_cold_start(last_job_finished: Optional[float]) -> None:
    if last_job_finished is None or (time.time() - last_job_finished) > COLD_START_AFTER_IDLE:
        delay = random.uniform(COLD_START_MIN, COLD_START_MAX)
        log.info("cold start %.2fs", delay, extra={"event": "cold_start"})
        time.sleep(delay)


# ---------- main loop ----------
def process_job(r: redis.Redis, http: httpx.Client, job: dict, last_job_finished: Optional[float]) -> float:
    job_id = job["job_id"]
    started = time.time()
    log.info("claimed", extra={"event": "claimed", "job_id": job_id})
    # Capture the claim_token the coordinator mints at /jobs/claim. We
    # thread it through partial/complete so the coordinator can reject
    # a stale completer (e.g. the reaper requeued and another worker
    # has since picked the job up). The in-VPS worker BLPOPs jobs
    # straight from Redis rather than going through /jobs/next, so
    # /jobs/claim is still its first network contact with the
    # coordinator for the job.
    claim_resp = post(
        http, "/jobs/claim",
        {"worker_id": WORKER_ID, "job_id": job_id},
    )
    claim_token: Optional[str] = (claim_resp or {}).get("claim_token")

    def push_partial(text: str) -> None:
        # Fire-and-forget: a dropped partial isn't worth retrying since
        # the next one carries the full text anyway, and the final
        # /jobs/complete is the source of truth.
        body = {"worker_id": WORKER_ID, "job_id": job_id, "text": text}
        if claim_token is not None:
            body["claim_token"] = claim_token
        post(http, "/jobs/partial", body, timeout=3)

    try:
        maybe_simulate_cold_start(last_job_finished)
        simulate_network_delay()
        tool = (job.get("tool") or "chat").lower()
        if tool == "search":
            result = run_search_inference(
                job["prompt"], job, http, on_partial=push_partial,
            )
        else:
            result = run_inference(
                job["prompt"],
                job.get("model"),
                http,
                on_partial=push_partial,
                messages=job.get("messages"),
            )
        duration = round(time.time() - started, 3)
        complete_body = {
            "worker_id": WORKER_ID,
            "job_id": job_id,
            "text": result["text"],
            "model": result["model"],
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "duration_seconds": duration,
            "status": "complete",
        }
        if result.get("sources"):
            complete_body["sources"] = result["sources"]
        if claim_token is not None:
            complete_body["claim_token"] = claim_token
        post(http, "/jobs/complete", complete_body, timeout=30)
        log.info(
            "complete tokens=%d duration=%.2fs",
            result["completion_tokens"],
            duration,
            extra={"event": "complete", "job_id": job_id},
        )
    except Exception as e:
        duration = round(time.time() - started, 3)
        log.exception("job failed: %s", e, extra={"event": "error", "job_id": job_id})
        complete_body = {
            "worker_id": WORKER_ID,
            "job_id": job_id,
            "text": "",
            "model": job.get("model") or MODEL,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_seconds": duration,
            "status": "error",
            "error": str(e),
        }
        if claim_token is not None:
            complete_body["claim_token"] = claim_token
        post(http, "/jobs/complete", complete_body, timeout=30)
    return time.time()


def main() -> None:
    log.info(
        "starting model=%s mock=%s availability=%s",
        MODEL, MOCK_INFERENCE, AVAILABILITY_WINDOW,
        extra={"event": "starting"},
    )
    r = connect_redis()
    http = httpx.Client(headers=auth_headers())
    register(http)

    last_heartbeat = 0.0
    last_job_finished: Optional[float] = None

    while True:
        now = time.time()

        if not is_available():
            if now - last_heartbeat > HEARTBEAT_INTERVAL:
                heartbeat(http, "offline")
                last_heartbeat = now
            time.sleep(min(POLL_INTERVAL * 2, 10))
            continue

        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            heartbeat(http, "idle")
            last_heartbeat = now

        # BLPOP across every queue we advertise so a search job
        # doesn't sit on job_queue:search waiting for a poll cycle
        # while this worker is idle.
        blpop_queues = [job_queue_for(t) for t in WORKER_TOOLS]
        item = r.blpop(blpop_queues, timeout=int(POLL_INTERVAL))
        if not item:
            continue

        _, raw = item
        try:
            job = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("dropping malformed job payload")
            continue

        heartbeat(http, "busy")
        last_job_finished = process_job(r, http, job, last_job_finished)
        heartbeat(http, "idle")
        last_heartbeat = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutdown")
