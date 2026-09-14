"""Context-aware image-prompt and search-query rewrite pipelines.

When a user submits an image inside an existing conversation, their new
request often references prior turns ("draw some corn" → image → "I
wanted it wrapped in bacon"). Sending "wrapped in bacon" to sd.cpp
produces a strip of bacon. The rewrite pipeline runs a hidden chat job
through Ollama that combines the recent conversation with the new
request and produces a self-contained image prompt ("corn wrapped in
bacon"). The image worker never sees the original request, only the
rewritten one. Side-effects: ~5s latency added to any image-with-context
submission; one extra chat job billed to the submitter. Skipped when: no
conversation, empty conversation, no chat workers online.

The search-query rewrite pipeline is the same plumbing applied to
tool=search: a routing classifier decides NO_SEARCH vs. a rewritten
query before the job reaches the search queue.

``db`` (and the still-main.py-local ``_read_heartbeat`` /
``_worker_advertises_tool`` helpers) is closed over via
``build_rewrite_helpers(...)`` rather than imported — same
closure-over-dependencies shape coordinator/notifications.py already
uses for ``db``. ``r`` is captured as ``get_r`` — a zero-arg getter,
``lambda: r`` — rather than the object itself: several test modules
reassign ``coordinator.main.r`` to a fakeredis instance *after*
importing main (see e.g. tests/test_conversations.py), which only a
live lookup at call time observes; capturing ``r`` by value at import
time would silently keep using whatever redis client existed at
import (real, in those tests, since it swaps in before the fake).

Unlike a ``build_router``, this returns the bare callables
``generate()``, ``/jobs/complete``, and the message-building helpers
call directly, unpacked back into the exact same names main.py already
used as bare globals:

    (
        _format_rewrite_history, _conversation_has_prior_context,
        _chat_worker_available_for_rewrite, _enqueue_chat_rewrite_for_image,
        _dispatch_image_after_rewrite, _parse_search_rewrite_output,
        _enqueue_chat_rewrite_for_search, _dispatch_search_after_rewrite,
        _scrub_citations,
    ) = prompt_rewrite.build_rewrite_helpers(
        db, lambda: r, _read_heartbeat, _worker_advertises_tool,
    )
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Optional

from shared.config import (
    IMAGE_REWRITE_PENDING,
    SEARCH_AUTO_DISABLED,
    SEARCH_REWRITE_PENDING,
    WORKER_HEARTBEATS,
    WORKER_TIMEOUT_SECONDS,
    job_queue_for,
)

log = logging.getLogger("coordinator.prompt_rewrite")

_REWRITE_HISTORY_LIMIT = 6

# Sentinel returned by _parse_search_rewrite_output when the classifier
# says no search is needed. Constant rather than magic string so the
# dispatcher and the tests can refer to the same value.
SEARCH_REWRITE_NO_SEARCH = "__no_search__"

_CITATION_PATTERN = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")


def _scrub_citations(text: str) -> str:
    """Strip ``[1]``, ``[2]``, ``[1, 2]``-style citation markers from
    an assistant message body. Used when we reroute a search job to
    the chat queue (NO_SEARCH classification): the original chat
    model produced citations backed by sources, but the chat reroute
    won't have those sources, and a small model seeing dangling
    citation markers tends to recant ("I made an error, I don't have
    sources for that"). Scrubbing leaves the prose intact and the
    follow-up has a clean context to respond from."""
    if not text:
        return text
    return _CITATION_PATTERN.sub("", text).strip()


def _format_rewrite_history(prior_messages) -> str:
    """Build a short text snippet representing recent conversation
    state for the rewriter. Limited to the most recent
    ``_REWRITE_HISTORY_LIMIT`` non-empty / non-pending messages so the
    chat model isn't asked to digest 200 turns. Image-bubble
    placeholders (``[image: foo]``) are rewritten as
    ``[generated image: foo]`` so the model understands what the
    assistant did instead of seeing the prompt repeated verbatim.

    Citation markers from prior search-grounded answers are scrubbed
    so the classifier reads clean prose — a 1-3B model squinting at
    a transcript full of ``[1][2]`` markers tends to treat the
    conversation as already-resolved and over-classify as
    NO_SEARCH, which is the exact misfire that motivates the
    enclosing rewrite pipeline."""
    relevant = [
        m for m in prior_messages
        if (m["text"] or "").strip() and m["status"] != "pending"
    ]
    recent = relevant[-_REWRITE_HISTORY_LIMIT:]
    lines = []
    for m in recent:
        role_label = "User" if m["role"] == "user" else "Assistant"
        text = (m["text"] or "").strip()
        if m["role"] == "assistant" and text.startswith("[image:"):
            inner = text[len("[image:"):].rstrip("]").strip()
            text = f"[generated image: {inner}]"
        elif m["role"] == "assistant":
            text = _scrub_citations(text)
        lines.append(f"{role_label}: {text}")
    return "\n".join(lines)


def _build_rewrite_meta_prompt(history: str, user_prompt: str) -> str:
    # The rewriter has to infer intent, not just splice tokens — the
    # "Canned corn → 'Yes' → Label Green Giant" failure showed up
    # because the previous version literally told the model to
    # "combine" the new request with the prior subject. Real-world
    # follow-ups are more varied: agreement with a prior assistant
    # suggestion, redirection, modification, fresh standalone. Each
    # gets its own paragraph in the instructions plus a worked
    # example so a 1-3B chat model can actually pick the right move.
    return (
        "You craft image-generation prompts from conversational context.\n\n"
        "RECENT CONVERSATION:\n"
        f"{history}\n\n"
        f"NEW USER MESSAGE: {user_prompt}\n\n"
        "Decide what image the user wants, then write a clear visual "
        "prompt for the image generator. Follow these intent-reading "
        "rules:\n\n"
        "1. If the user says \"yes\", \"ok\", \"that one\", \"do it\", "
        "or similar agreement and the assistant's MOST RECENT message "
        "contains a visual description or image suggestion: write a "
        "prompt that captures the SUBSTANCE of that description "
        "(subject, setting, visible details). Do not just echo a "
        "short label — pull out the actual visual content the "
        "assistant described.\n"
        "2. If the user says something like \"make it bigger\", \"but "
        "blue\", \"wrapped in bacon\", \"on a beach instead\": that's "
        "a modification of the most recent image. Combine the prior "
        "subject with the new detail.\n"
        "3. If the user describes a NEW subject from scratch (e.g. "
        "\"a sunset over the ocean\"): use it as-is, ignore prior "
        "context.\n"
        "4. If the user gives a fragment that depends on prior "
        "context (e.g. \"with a label\" after \"canned corn\"): "
        "combine.\n\n"
        "Style requirements for your output prompt:\n"
        "- 1-3 sentences. Be concrete and visual: name the subject, "
        "the setting, key features, materials, lighting. Length "
        "should match how much context the user gave you — short "
        "if they gave little, rich if they gave a lot.\n"
        "- Skip storytelling, brand disclaimers, refusals, or "
        "meta-commentary. Just describe the image.\n"
        "- Do not wrap in quotes. Do not include labels like "
        "\"PROMPT:\" or \"Image:\". Output the prompt only.\n\n"
        "PROMPT:"
    )


def _clean_rewritten_prompt(raw: Optional[str], fallback: str) -> str:
    """Defensive cleanup on model output: trim, strip wrapping quotes,
    strip echoed headers, collapse internal blank lines, and refuse
    on suspiciously empty / oversized output by falling back to the
    original prompt.

    Multi-line output is preserved (sd.cpp accepts multi-sentence
    prompts and the richer ones produce better images) — the previous
    first-line-only cut was throwing away the descriptive detail the
    intent-aware meta-prompt is now asking the model to produce."""
    if not raw:
        return fallback
    s = raw.strip()
    # Strip any echoed header the model parrots back from the
    # meta-prompt. Loop so e.g. "PROMPT:\nFINAL PROMPT:" gets fully
    # peeled.
    for _ in range(3):
        stripped = False
        for header in (
            "PROMPT:", "FINAL PROMPT:", "Final prompt:",
            "Rewritten:", "Rewritten prompt:", "Output:",
            "Image:", "IMAGE PROMPT:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    # Collapse runs of blank lines but keep paragraph structure —
    # sd.cpp parses the whole string, not just line one.
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    if not s or len(s) > 500:
        return fallback
    return s


def _conversation_has_prior_context(prior_messages) -> bool:
    """A conversation has 'context' for rewrite purposes iff there's at
    least one prior completed message in it (any role). An empty
    conversation or one with only the just-inserted pending pair has
    nothing for the rewriter to work with — skip and use the raw
    prompt. This check runs BEFORE the new user+pending-assistant pair
    is appended in /generate, so ``prior_messages`` is the pre-insert
    snapshot."""
    return any(
        (m["text"] or "").strip() and m["status"] != "pending"
        for m in prior_messages
    )


def _build_search_query_meta_prompt(history: str, user_prompt: str) -> tuple[str, str]:
    """Return ``(system_message, user_message)`` for the routing
    classifier. We feed Ollama via /api/chat (messages[]) instead of
    /api/generate (raw prompt) so the small chat model's instruction-
    tuning chat template kicks in and the model treats this as
    "follow instructions" instead of "continue text".

    The drift from /api/generate was killing reliability — manual
    testing of "for sure it's happening?" / "really?" / "For sure?"
    on three consecutive turns produced three different failure
    modes (NO_SEARCH sentinel for the wrong reason, panic recant
    text, and a verbatim echo of the user prompt) when the same
    instructions were sent as a single completion prompt. Splitting
    rules+examples into the system slot and the per-call data into
    the user slot is the standard fix.

    Examples that motivated the design:
    - User: "news"           → "today's top news headlines"
    - User: "try again"      + prior news topic → "different recent news event"
    - User: "That's cool!"   + prior Boring Co topic → NO_SEARCH
    - User: "thanks!"        + any context       → NO_SEARCH
    - User: "for sure?"      + prior project topic → "<project> confirmed"
    """
    system = (
        "You are a STRICT routing classifier for a web-search "
        "assistant. Your ONLY job is to output one of two things:\n"
        "  (a) the literal token NO_SEARCH, or\n"
        "  (b) a short web-search query (3-8 keywords).\n"
        "Nothing else. No explanations. No apologies. No prose. No "
        "quoted answers to the user. You are not the user-facing "
        "assistant — you are a classifier whose output goes into a "
        "search engine.\n\n"
        "HARD RULES (apply FIRST):\n"
        "  1. If the new user message contains \"?\" → output a "
        "query. NEVER NO_SEARCH.\n"
        "  2. If the new user message expresses doubt, verification, "
        "or asks for confirmation → output a query. NEVER NO_SEARCH.\n"
        "  3. Default is QUERY. Only output NO_SEARCH for a pure ack "
        "with NO question and NO doubt.\n\n"
        "QUERY CRAFT:\n"
        "- Fragment that depends on prior context: use the prior "
        "topic to fill in the missing subject.\n"
        "- Standalone topic: tighten into keywords. Drop conversational "
        "filler.\n"
        "- Latest/newest? Add \"today\" / \"latest\" / the current year.\n"
        "- 3-8 keywords. No quotes. No labels.\n\n"
        "WORKED EXAMPLES:\n\n"
        "Example 1 — verification of a prior topic:\n"
        "  RECENT CONVERSATION:\n"
        "  User: Kevin O'Leary Utah data center news\n"
        "  Assistant: A $70B project has been announced...\n"
        "  NEW USER MESSAGE: for sure it's happening?\n"
        "  OUTPUT: kevin oleary utah data center confirmed status\n\n"
        "Example 2 — short doubt:\n"
        "  RECENT CONVERSATION:\n"
        "  User: SVB collapse rundown\n"
        "  Assistant: Silicon Valley Bank failed in March 2023...\n"
        "  NEW USER MESSAGE: really?\n"
        "  OUTPUT: SVB silicon valley bank collapse confirmed\n\n"
        "Example 3 — pure ack:\n"
        "  RECENT CONVERSATION:\n"
        "  User: NBA scores yesterday\n"
        "  Assistant: Lakers beat the Suns 110-102...\n"
        "  NEW USER MESSAGE: thanks!\n"
        "  OUTPUT: NO_SEARCH\n\n"
        "Example 4 — drill-in:\n"
        "  RECENT CONVERSATION:\n"
        "  User: banking news today\n"
        "  Assistant: Major US banks reported earnings...\n"
        "  NEW USER MESSAGE: what about Europe?\n"
        "  OUTPUT: european banking news today\n\n"
        "Example 5 — verification with question mark:\n"
        "  RECENT CONVERSATION:\n"
        "  User: latest iPhone\n"
        "  Assistant: The iPhone 17 was released...\n"
        "  NEW USER MESSAGE: are you sure that's the latest?\n"
        "  OUTPUT: latest iPhone model 2026\n"
    )
    user = (
        "RECENT CONVERSATION:\n"
        f"{history}\n\n"
        f"NEW USER MESSAGE: {user_prompt}\n\n"
        "OUTPUT:"
    )
    return (system, user)


def _parse_search_rewrite_output(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """Parse the rewrite chat job's output into one of:
    - ("skip", None) — the model decided no search is needed
    - ("query", "<cleaned query>") — the model produced a query
    - ("error", None) — output was empty / nonsense; caller should
      fall back to the original prompt

    Splitting parse from clean lets the dispatcher branch on intent
    without smuggling sentinels through the existing clean function."""
    if not raw:
        return ("error", None)
    s = raw.strip()
    # Strip any echoed header so "OUTPUT: NO_SEARCH" also classifies.
    for _ in range(3):
        stripped = False
        for header in (
            "OUTPUT:", "Output:", "QUERY:", "Query:", "Search query:",
            "Search:", "FINAL QUERY:", "Rewritten query:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    # NO_SEARCH detection — first-line, case-insensitive. We accept
    # the sentinel anywhere on the first line so "NO_SEARCH (the user
    # is just thanking me)" still classifies as skip.
    first_line = s.split("\n", 1)[0].strip().upper()
    if first_line.startswith("NO_SEARCH") or first_line == "NOSEARCH":
        return ("skip", None)
    return ("query", s)


def _clean_rewritten_search_query(raw: Optional[str], fallback: str) -> str:
    """Same defensive cleanup as `_clean_rewritten_prompt` but with a
    tighter length budget (search queries should be SHORT — anything
    over 200 chars is the model rambling) and an additional pass that
    flattens multi-line output to a single line. DDG accepts long
    queries but treats every extra word as a hard requirement, which
    is the opposite of what we want for a paraphrased follow-up."""
    if not raw:
        return fallback
    s = raw.strip()
    for _ in range(3):
        stripped = False
        for header in (
            "QUERY:", "Query:", "Search query:", "Search:",
            "FINAL QUERY:", "Rewritten query:", "Output:",
        ):
            if s.lower().startswith(header.lower()):
                s = s[len(header):].strip()
                stripped = True
                break
        if not stripped:
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    # First line only — a multi-line query is the model writing prose,
    # which DDG won't handle. The first line is almost always the
    # actual query.
    s = s.split("\n", 1)[0].strip()
    if not s or len(s) > 200:
        return fallback
    return s


def build_rewrite_helpers(db, get_r, read_heartbeat_fn, worker_advertises_tool_fn):
    """Close over db/get_r()/the still-main.py-local heartbeat helpers
    and return the functions generate() / /jobs/complete / retry_message
    call directly, in a fixed order the caller unpacks back into the
    same bare names main.py already used as module globals."""

    def _chat_worker_available_for_rewrite() -> bool:
        """Is any chat-capable worker heartbeating recently? Without one,
        the rewrite chat job would sit on the queue forever and the user
        would never get their image, so we skip the rewrite step entirely
        and fall back to the raw prompt."""
        now = time.time()
        heartbeats = get_r().hgetall(WORKER_HEARTBEATS) or {}
        for worker_id, _raw in heartbeats.items():
            ts, _ = read_heartbeat_fn(worker_id)
            if not ts or (now - ts) > WORKER_TIMEOUT_SECONDS:
                continue
            if worker_advertises_tool_fn(worker_id, "chat"):
                return True
        return False

    def _enqueue_chat_rewrite_for_image(
        image_job_id: str,
        image_envelope: dict,
        original_prompt: str,
        history: str,
        submitted_by: Optional[str],
        submitted_at: float,
    ) -> str:
        """Enqueue the hidden chat rewrite job, store the linkage in
        Redis, and return the rewrite job_id. The actual image dispatch
        happens later, in /jobs/complete, when the rewrite returns.

        Linkage Redis HSET happens BEFORE the queue push so a fast worker
        that completes the rewrite job in microseconds can't race ahead of
        the linkage write and find no dispatch instructions."""
        rewrite_job_id = str(uuid.uuid4())
        meta_prompt = _build_rewrite_meta_prompt(history, original_prompt)
        rewrite_envelope = {
            "job_id": rewrite_job_id,
            "prompt": meta_prompt,
            # No `model` — worker uses its default chat model. No
            # `conversation_id` — this job is invisible to the chat UI.
            "submitted_at": submitted_at,
            "tool": "chat",
        }
        db.insert_job(
            rewrite_job_id,
            meta_prompt,
            None,
            submitted_at,
            submitted_by,
            conversation_id=None,
            tool="chat",
        )
        get_r().hset(
            IMAGE_REWRITE_PENDING,
            rewrite_job_id,
            json.dumps({
                "image_job_id": image_job_id,
                "image_envelope": image_envelope,
                "original_prompt": original_prompt,
            }),
        )
        get_r().rpush(job_queue_for("chat"), json.dumps(rewrite_envelope))
        log.info(
            "image prompt rewrite enqueued",
            extra={
                "event": "rewrite_enqueued",
                "rewrite_job_id": rewrite_job_id,
                "image_job_id": image_job_id,
            },
        )
        return rewrite_job_id

    def _dispatch_image_after_rewrite(
        rewrite_job_id: str,
        link: dict,
        rewritten_text: Optional[str],
    ) -> None:
        """Called from /jobs/complete when the rewrite chat job finishes.
        Plugs the cleaned rewritten text into the stored image envelope,
        flips the image job row out of 'awaiting_rewrite', and RPUSHes the
        image onto the real image queue.

        Skipped (with the link still cleaned up) when the image job is
        no longer in 'awaiting_rewrite' status — covers the cancel-while-
        rewriting race (user clicked Cancel after we enqueued the rewrite
        but before it completed) and the conversation-was-purged race
        (image job row deleted while in flight). Without this guard we'd
        push an image onto the queue that no one is watching anymore.

        Always deletes the link entry so a stale duplicate completion (or
        a future replay) can't re-trigger dispatch."""
        image_job_id = link["image_job_id"]
        image_envelope = link["image_envelope"]
        original = link["original_prompt"]
        try:
            image_row = db.get_job(image_job_id)
            if image_row is None or image_row["status"] != "awaiting_rewrite":
                get_r().hdel(IMAGE_REWRITE_PENDING, rewrite_job_id)
                log.info(
                    "image rewrite finished but image job no longer awaiting "
                    "rewrite — skipping dispatch",
                    extra={
                        "event": "rewrite_dispatch_skipped",
                        "rewrite_job_id": rewrite_job_id,
                        "image_job_id": image_job_id,
                        "image_status": image_row["status"] if image_row else "missing",
                    },
                )
                return
            final_prompt = _clean_rewritten_prompt(rewritten_text, original)
            image_envelope["prompt"] = final_prompt
            db.set_job_pending_with_prompt(image_job_id, final_prompt)
            get_r().rpush(job_queue_for("image"), json.dumps(image_envelope))
            log.info(
                "image dispatched after rewrite",
                extra={
                    "event": "rewrite_dispatched",
                    "rewrite_job_id": rewrite_job_id,
                    "image_job_id": image_job_id,
                    "rewrite_was_used": final_prompt != original,
                },
            )
        finally:
            get_r().hdel(IMAGE_REWRITE_PENDING, rewrite_job_id)

    def _enqueue_chat_rewrite_for_search(
        search_job_id: str,
        search_envelope: dict,
        original_prompt: str,
        history: str,
        submitted_by: Optional[str],
        submitted_at: float,
    ) -> str:
        """Same plumbing as `_enqueue_chat_rewrite_for_image` but linkage
        lives in SEARCH_REWRITE_PENDING and the eventual dispatch lands
        on `job_queue:search` instead of `job_queue:image`. The two
        rewrite types are stored under separate hashes so /jobs/complete
        knows which dispatcher to call without inspecting the original
        job's tool field."""
        rewrite_job_id = str(uuid.uuid4())
        system_msg, user_msg = _build_search_query_meta_prompt(
            history, original_prompt,
        )
        # The worker prefers messages[] when present (routes to Ollama
        # /api/chat with the model's chat template applied) and falls
        # back to `prompt` for legacy clients / single-shot generations.
        # Both fields are populated for backward-compatibility, but the
        # chat path is the load-bearing one — without the chat template,
        # a 3B model treats this as "continue text" and produces weird
        # output (panic recants, verbatim echoes, etc.). The persisted
        # DB row stores the concatenated text as the canonical prompt
        # for ledger / admin debug purposes.
        persisted_prompt = system_msg + "\n\n---\n\n" + user_msg
        rewrite_envelope = {
            "job_id": rewrite_job_id,
            "prompt": user_msg,  # legacy fallback only
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "submitted_at": submitted_at,
            "tool": "chat",
        }
        db.insert_job(
            rewrite_job_id,
            persisted_prompt,
            None,
            submitted_at,
            submitted_by,
            conversation_id=None,
            tool="chat",
        )
        get_r().hset(
            SEARCH_REWRITE_PENDING,
            rewrite_job_id,
            json.dumps({
                "search_job_id": search_job_id,
                "search_envelope": search_envelope,
                "original_prompt": original_prompt,
            }),
        )
        get_r().rpush(job_queue_for("chat"), json.dumps(rewrite_envelope))
        log.info(
            "search query rewrite enqueued",
            extra={
                "event": "search_rewrite_enqueued",
                "rewrite_job_id": rewrite_job_id,
                "search_job_id": search_job_id,
                # Content logging — without these, diagnosis required
                # SSHing into prod and dumping the SQLite jobs table.
                "original_prompt": (original_prompt or "")[:200],
                "history_chars": len(history or ""),
            },
        )
        return rewrite_job_id

    def _dispatch_search_after_rewrite(
        rewrite_job_id: str,
        link: dict,
        rewritten_text: Optional[str],
    ) -> None:
        """Called from /jobs/complete when a search-rewrite chat job
        finishes. Plumbs three outcomes:

        a) The classifier says NO_SEARCH (user message was a closure like
           "thanks" / "that's cool!"). We DROP the search step entirely,
           reroute the job to the chat queue with the conversation
           context intact, and record a marker so /jobs/complete can
           stamp `search_was_skipped: true` on the result. The client
           uses that to auto-uncheck the search box and reset the
           sticky-mode state for this conversation.

        b) The classifier produced a query. Plug it into the stored
           search envelope, flip the job out of `awaiting_rewrite`,
           RPUSH onto the search queue. Same shape as
           `_dispatch_image_after_rewrite`.

        c) Parse error / empty rewrite. Fall back to the user's original
           prompt, send to the search queue. Conservative — better to
           give the user a literal-prompt search than a stuck bubble.

        Skips entirely (still cleans the link) if the search job is no
        longer awaiting_rewrite — the cancel-while-rewriting and
        conversation-purged races."""
        search_job_id = link["search_job_id"]
        search_envelope = link["search_envelope"]
        original = link["original_prompt"]
        try:
            search_row = db.get_job(search_job_id)
            if search_row is None or search_row["status"] != "awaiting_rewrite":
                log.info(
                    "search rewrite finished but search job no longer awaiting "
                    "rewrite — skipping dispatch",
                    extra={
                        "event": "search_rewrite_dispatch_skipped",
                        "rewrite_job_id": rewrite_job_id,
                        "search_job_id": search_job_id,
                        "search_status": search_row["status"] if search_row else "missing",
                    },
                )
                return

            decision, value = _parse_search_rewrite_output(rewritten_text)
            # Always log the raw rewrite output + parsed decision so any
            # future regression is diagnosable from logs alone — the prior
            # Kevin O'Leary bug required SSHing into prod and dumping the
            # SQLite jobs table to figure out the classifier was
            # producing panic recants instead of NO_SEARCH or a query.
            log.info(
                "search rewrite parsed",
                extra={
                    "event": "search_rewrite_parsed",
                    "rewrite_job_id": rewrite_job_id,
                    "search_job_id": search_job_id,
                    "original_prompt": (original or "")[:200],
                    "rewrite_output": (rewritten_text or "")[:200],
                    "decision": decision,
                    "parsed_value": (value or "")[:200] if value else None,
                },
            )

            # Hard guardrail: a question is never a closure. If the user's
            # original prompt contains a "?", we ignore a SKIP decision
            # and force a query. This catches a real failure observed in
            # manual testing where "for sure it's happening?" (a follow-
            # up VERIFICATION question) was misclassified as NO_SEARCH —
            # the chat reroute then panic-recanted the prior search-
            # grounded answer. Meta-prompt fix is also in place; the
            # guardrail is belt-and-suspenders.
            if decision == "skip" and "?" in (original or ""):
                log.info(
                    "search rewrite skip overridden by ? guardrail",
                    extra={
                        "event": "search_rewrite_skip_overridden",
                        "rewrite_job_id": rewrite_job_id,
                        "search_job_id": search_job_id,
                        "original_prompt": (original or "")[:200],
                    },
                )
                decision = "error"  # fall through to original-prompt path
                value = None

            if decision == "skip":
                # Reverse-detection branch: reroute as plain chat. Strip
                # the search-specific fields so the worker treats it as a
                # normal chat job with the conversation history.
                #
                # Citation markers in prior assistant turns are scrubbed
                # — without sources to back them up, a small chat model
                # will see "[1][2]" and panic-recant ("I made an error,
                # I don't have sources for that claim"). The actual
                # information in the prior turn is fine to keep; just
                # the citation hooks need to go. Caught in manual testing
                # by the "for sure it's happening?" → recanted-prior-
                # answer failure.
                chat_envelope = dict(search_envelope)
                chat_envelope.pop("search", None)
                chat_envelope["tool"] = "chat"
                chat_envelope["prompt"] = original
                if chat_envelope.get("messages"):
                    chat_envelope["messages"] = [
                        {
                            "role": m["role"],
                            "content": _scrub_citations(m.get("content", "")),
                        }
                        for m in chat_envelope["messages"]
                    ]
                db.set_job_pending_with_prompt(search_job_id, original)
                get_r().hset(SEARCH_AUTO_DISABLED, search_job_id, "1")
                get_r().rpush(job_queue_for("chat"), json.dumps(chat_envelope))
                log.info(
                    "search disabled by classifier — rerouted to chat",
                    extra={
                        "event": "search_rewrite_classified_skip",
                        "rewrite_job_id": rewrite_job_id,
                        "search_job_id": search_job_id,
                        "original_prompt": (original or "")[:200],
                    },
                )
                return

            # Query or fall-back-to-original.
            if decision == "query":
                final_query = _clean_rewritten_search_query(value, original)
            else:
                final_query = original
            search_envelope["prompt"] = final_query
            db.set_job_pending_with_prompt(search_job_id, final_query)
            get_r().rpush(job_queue_for("search"), json.dumps(search_envelope))
            log.info(
                "search dispatched after rewrite",
                extra={
                    "event": "search_rewrite_dispatched",
                    "rewrite_job_id": rewrite_job_id,
                    "search_job_id": search_job_id,
                    "rewrite_was_used": final_query != original,
                    "original_prompt": (original or "")[:200],
                    "final_query": (final_query or "")[:200],
                    "decision": decision,
                },
            )
        finally:
            get_r().hdel(SEARCH_REWRITE_PENDING, rewrite_job_id)

    return (
        _format_rewrite_history,
        _conversation_has_prior_context,
        _chat_worker_available_for_rewrite,
        _enqueue_chat_rewrite_for_image,
        _dispatch_image_after_rewrite,
        _parse_search_rewrite_output,
        _enqueue_chat_rewrite_for_search,
        _dispatch_search_after_rewrite,
        _scrub_citations,
    )
