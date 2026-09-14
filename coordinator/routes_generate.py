"""The core public API: /health, /tos, /tos/raw, /generate, /result.

``db``/``idem`` are closed over directly (same shape
coordinator/notifications.py already uses for ``db``); ``r`` as
``get_r`` (a zero-arg getter — see coordinator/prompt_rewrite.py for
why). ``ensure_live_worker_fn`` / ``ensure_capacity_fn`` because
``_ensure_live_worker_or_503`` / ``_ensure_capacity_or_503`` still live
in coordinator/main.py's shared helpers section. ``tos_version`` /
``load_tos_text_fn`` because ``TOS_VERSION`` / ``_load_tos_text`` still
live in main.py's community-ToS section.
``chat_worker_available_for_rewrite_fn`` /
``enqueue_chat_rewrite_for_image_fn`` / ``enqueue_chat_rewrite_for_search_fn``
are coordinator/main.py-local names bound from
``coordinator.prompt_rewrite.build_rewrite_helpers(...)``'s own return
tuple — not directly importable from prompt_rewrite.py itself (they're
closures there too).

``build_router`` returns ``(router, generate, result, job_row_to_envelope)``
rather than a bare router: ``coordinator/openai_compat.py`` needs
``generate``/``result`` as injected callables (same reasoning as
``db`` elsewhere — it never imports anything from this module), and
``coordinator/routes_workers.py``'s ``/jobs/claim``/``/jobs/abandon``
need ``_job_row_to_envelope`` as ``job_row_to_envelope_fn``. main.py
wires it once, BEFORE routes_workers.py (which needs the returned
``job_row_to_envelope``):

    _generate_router, generate, result, _job_row_to_envelope = routes_generate.build_router(
        db, lambda: r, idem, _ensure_live_worker_or_503, _ensure_capacity_or_503,
        TOS_VERSION, _load_tos_text, _chat_worker_available_for_rewrite,
        _enqueue_chat_rewrite_for_image, _enqueue_chat_rewrite_for_search,
    )
    app.include_router(_generate_router)
    app.include_router(api_keys.build_router(db))
    app.include_router(openai_compat.build_router(db, generate, result, model_registry))
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from string import Template as _Template
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from coordinator import model_registry
from coordinator import uploads as uploads_lib
from coordinator.image_moderation import (
    _image_prompt_is_blocked,
    _validate_and_classify_init_image,
)
from coordinator.image_params import (
    _clamp_image_dim,
    _combine_negative_prompt,
    _default_image_params,
)
from coordinator.prompt_rewrite import (
    _conversation_has_prior_context,
    _format_rewrite_history,
    _scrub_citations,
)
from coordinator.routes_conversations import _require_conversation_owner
from coordinator.tiers import quota_for as _tier_quota_for
from shared.auth import AUTH_ENABLED
from shared.config import (
    CANARY_REAL_JOBS_SINCE,
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    JOB_RESULTS,
    MAX_HISTORY_TOKENS,
    MAX_PROMPT_BYTES,
    STRICT_MODELS,
    SUMMARY_PENDING,
    job_queue_for,
)
from shared.models import GenerateRequest, GenerateResponse
from shared.ui import BASE_CSS as _BASE_CSS, VIEWPORT_META as _VIEWPORT_META

log = logging.getLogger("coordinator.routes_generate")

_TOS_CSS = """
.page { max-width: 760px; line-height: 1.6; }
.meta { color: var(--muted); font-size: .9rem; margin-bottom: 1.5rem; padding-bottom: .75rem; border-bottom: 1px solid var(--border-soft); }
#content h1 { margin-top: 0; }
#content h2 { margin-top: 2rem; font-size: 1.25rem; }
#content h3 { margin-top: 1.5rem; font-size: 1.05rem; color: #333; }
#content p { margin: .75rem 0; }
#content ul, #content ol { padding-left: 1.25rem; margin: .5rem 0 .75rem; }
#content li { margin-bottom: .25rem; }
#content em { color: var(--muted); }
#content hr { border: 0; border-top: 1px solid var(--border); margin: 1.75rem 0; }
#loading { color: var(--muted); }
"""

_TOS_HTML_TEMPLATE = _Template(
    '<!doctype html><html><head><meta charset="utf-8">'
    + _VIEWPORT_META
    + "<title>GamerAI — Community ToS</title>"
    + "<style>" + _BASE_CSS + _TOS_CSS + "</style></head>"
    + '<body><div class="page">'
    + '<h1><a href="/">GamerAI</a></h1>'
    + '<div class="meta">Version <strong>$version</strong> · '
      '<a href="/tos/raw">view raw</a></div>'
    + '<div id="content"><span id="loading">Loading terms…</span></div>'
    + '<script src="/static/marked.min.js"></script>'
    + '<script src="/static/purify.min.js"></script>'
    + "<script>"
      "fetch('/tos/raw').then(r => r.text()).then(md => {"
      "  const html = window.marked.parse(md);"
      "  document.getElementById('content').innerHTML ="
      "    window.DOMPurify ? window.DOMPurify.sanitize(html) : html;"
      "}).catch(() => {"
      "  document.getElementById('content').innerHTML ="
      "    '<p>Could not load terms. <a href=\"/tos/raw\">View raw markdown</a>.</p>';"
      "});"
      "</script>"
    + "</div></body></html>"
)

_SUMMARY_SYSTEM_PROMPT = (
    "You produce CONVERSATION RECAPS for a chat assistant. Your "
    "output is prepended to the next reply's context so the "
    "assistant remembers who they're talking to and what they "
    "discussed — it is NOT a content summary of any document, "
    "article, code, or text that happens to appear in the "
    "transcript.\n\n"
    "Capture: what the user is working on or interested in, "
    "personal details they shared (name, location, preferences, "
    "projects), questions they asked, decisions or opinions they "
    "expressed, and any unresolved threads. If a document or piece "
    "of content came up, just note that it came up — do not "
    "summarize the document itself. Drop pleasantries, restated "
    "questions, and any quoted/generated text. Write 2-3 short "
    "paragraphs of plain prose as if briefing a colleague taking "
    "over the conversation."
)


def _estimate_history_tokens(text: str) -> int:
    """Coarse token estimator for the history-cap math. chars/4 lines up
    with how the worker bills tokens elsewhere; perfect parity with the
    model's tokenizer isn't necessary because the cap is a soft target
    aimed at "submit-to-first-token doesn't grow O(history)", not a
    hard quota."""
    return max(1, len(text) // 4) if text else 0


def _build_chat_messages_with_info(
    prior_messages,
    new_user_text: str,
    summary_text: Optional[str] = None,
    summary_through_seq: Optional[int] = None,
    cap_tokens: int = MAX_HISTORY_TOKENS,
    document_context: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """Build the Ollama /api/chat messages[] array from persisted
    conversation rows plus the new user turn, applying a tail-window
    cap so a many-turn thread doesn't pin model prefill to O(history).

    Newest turns are kept verbatim. Once the accumulated estimate hits
    ``cap_tokens`` we stop folding in older turns. If a ``summary_text``
    is supplied it's prepended as a system message and any persisted
    turn with seq <= summary_through_seq is excluded (those turns are
    represented by the summary). Returns the messages array AND an
    info dict the response can surface to the client so the UI can
    display "older turns aren't in context".

    ``document_context`` (from coordinator.uploads.build_document_context)
    is inserted as its own system message immediately before the new
    user turn — deliberately NOT subject to cap_tokens/MAX_HISTORY_TOKENS,
    since an attached document is current-turn context to answer against,
    not aging history to be pruned; it has its own independent budget
    (MAX_UPLOAD_CONTEXT_CHARS, applied by the caller).

    Pending/empty assistant rows from a previous-failed-but-not-yet-
    retried turn are skipped so the model doesn't see a stray empty-
    assistant message in the middle of the history.

    Citation markers (``[1]``, ``[2, 3]``) are scrubbed from prior
    assistant content — see _scrub_citations for the bug they caused
    when handed back to a model alongside a new search step's sources."""
    # Filter + normalize, keeping seq so we can apply summary_through_seq.
    eligible: list[dict] = []
    for m in prior_messages:
        role = m["role"]
        text = (m["text"] or "").strip()
        status = m["status"] if "status" in m.keys() else "complete"
        seq = m["seq"] if "seq" in m.keys() else None
        if role not in ("user", "assistant", "system"):
            continue
        if role == "assistant" and (status != "complete" or not text):
            continue
        if (
            summary_through_seq is not None
            and seq is not None
            and seq <= summary_through_seq
        ):
            # Replaced by the summary; do not include the raw turn too.
            continue
        if role == "assistant":
            text = _scrub_citations(text)
        eligible.append({"role": role, "content": text, "seq": seq})

    # Walk newest → oldest, keeping turns until the cap is hit. Stop at
    # the first overshoot so we don't half-include a long turn (e.g.,
    # a 3000-token paste). The newest turn always lands even if it
    # alone exceeds cap_tokens — dropping it would defeat the point.
    kept_rev: list[dict] = []
    tokens_used = 0
    for m in reversed(eligible):
        cost = _estimate_history_tokens(m["content"])
        if kept_rev and tokens_used + cost > cap_tokens:
            break
        tokens_used += cost
        kept_rev.append(m)
    kept = list(reversed(kept_rev))
    dropped_count = len(eligible) - len(kept)
    tokens_dropped = sum(
        _estimate_history_tokens(m["content"])
        for m in eligible[: len(eligible) - len(kept)]
    )

    out: list[dict] = []
    if summary_text:
        # Anchor the model with the earlier-history summary first so it
        # has continuity without paying the full token cost. Phrased as
        # a system message because it's editorial context, not user or
        # assistant words.
        out.append({
            "role": "system",
            "content": (
                "Earlier in this conversation (summarized):\n" + summary_text
            ),
        })
    for m in kept:
        out.append({"role": m["role"], "content": m["content"]})
    if document_context:
        # Placed right before the current turn (not up top with the
        # summary) so the model's attention lands on it next to the
        # question it's actually needed for.
        out.append({
            "role": "system",
            "content": (
                "The user has attached one or more documents to this "
                "conversation. Use them to answer when relevant:\n\n"
                + document_context
            ),
        })
    out.append({"role": "user", "content": (new_user_text or "").strip()})

    info = {
        "messages_total": len(eligible) + (1 if summary_text else 0),
        "messages_kept": len(kept),
        "messages_dropped": dropped_count,
        "tokens_kept": tokens_used,
        "tokens_dropped": tokens_dropped,
        "summary_in_use": bool(summary_text),
        "cap_tokens": cap_tokens,
    }
    return out, info


def _build_chat_messages(prior_messages, new_user_text: str) -> list[dict]:
    """Back-compat wrapper that discards the truncation info dict.
    Used by the requeue path, where there's no /generate response to
    surface stats on."""
    msgs, _info = _build_chat_messages_with_info(prior_messages, new_user_text)
    return msgs


def build_router(
    db, get_r, idem, ensure_live_worker_fn, ensure_capacity_fn,
    tos_version: str, load_tos_text_fn,
    chat_worker_available_for_rewrite_fn, enqueue_chat_rewrite_for_image_fn,
    enqueue_chat_rewrite_for_search_fn,
):
    router = APIRouter()

    def _maybe_enqueue_summary_job(conv_row, prior_messages, history_info) -> None:
        """Fire an async chat job that summarizes the oldest turns the
        truncation pass just dropped. The job runs through the normal
        worker pool; on /jobs/complete the result text replaces
        conversations.summary_text and bumps summary_through_seq, so the
        next /generate ships a short summary + recent turns instead of
        the full transcript. No-op when nothing fresh needs summarizing
        (no drops, or the existing summary already covers them)."""
        if not history_info:
            return
        if history_info.get("messages_dropped", 0) < 2:
            return
        conv_id = conv_row["conversation_id"]
        existing_through = (
            conv_row["summary_through_seq"]
            if "summary_through_seq" in conv_row.keys()
            else None
        ) or 0
        # Reconstruct the eligible-and-sorted view that the build path used,
        # so messages_dropped tracks the same chronologically-ordered set.
        eligible: list = []
        for m in prior_messages:
            role = m["role"]
            status = m["status"] if "status" in m.keys() else "complete"
            text = (m["text"] or "").strip()
            seq = m["seq"] if "seq" in m.keys() else None
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and (status != "complete" or not text):
                continue
            if seq is None:
                continue
            eligible.append(m)
        if not eligible:
            return
        eligible.sort(key=lambda m: m["seq"])
        dropped_count = history_info["messages_dropped"]
        dropped_msgs = eligible[:dropped_count]
        if not dropped_msgs:
            return
        new_through_seq = int(dropped_msgs[-1]["seq"])
        if new_through_seq <= existing_through:
            return  # already summarized this far
        # Build the summarizer's input. Originally this was the system
        # prompt + the raw user/assistant turns as separate messages, but
        # small models (llama3.2:3b) returned empty text when the message
        # array ended on an assistant turn — Ollama had no "what should I
        # say next?" cue. The conversation-as-single-user-message shape
        # gives the model an unambiguous "user asks for summary" turn to
        # respond to, which yields a non-empty assistant reply every time.
        existing_summary = (
            conv_row["summary_text"]
            if "summary_text" in conv_row.keys()
            else None
        )
        conv_lines: list[str] = []
        if existing_summary:
            conv_lines.append("Summary so far:\n" + existing_summary + "\n")
        for m in eligible:
            if m["seq"] > new_through_seq:
                break
            role = m["role"]
            text = (m["text"] or "").strip()
            if not text:
                continue
            if role == "assistant":
                text = _scrub_citations(text)
            label = "User" if role == "user" else "Assistant"
            conv_lines.append(f"{label}: {text}")
        conversation_text = "\n\n".join(conv_lines)
        summary_input: list[dict] = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Below is a conversation between a User and Assistant. "
                "Recap it in 2-3 short paragraphs so the next reply "
                "remembers the USER — what they want help with, who "
                "they are, what they've shared about themselves. Do "
                "NOT summarize any documents, articles, code, or text "
                "that appears in the transcript; just note that they "
                "came up (e.g. 'the user asked for the Declaration of "
                "Independence', not 'the Declaration of Independence "
                "is a foundational document…').\n\n"
                "--- BEGIN CONVERSATION ---\n\n"
                + conversation_text +
                "\n\n--- END CONVERSATION ---\n\n"
                "Provide only the recap text. No preamble, no headers, "
                "no lists, no meta commentary."
            )},
        ]
        # Orphan chat job — no conversation_id link, no submitter, no
        # placeholder message row. Runs through the same worker pool as
        # any other chat job; the worker can't tell it apart, which is
        # fine because the summary system prompt does all the steering.
        job_id = str(uuid.uuid4())
        submitted_at = time.time()
        envelope = {
            "job_id": job_id,
            "prompt": _SUMMARY_SYSTEM_PROMPT,
            "messages": summary_input,
            "model": None,
            "submitted_at": submitted_at,
            "tool": "chat",
        }
        db.insert_job(
            job_id,
            _SUMMARY_SYSTEM_PROMPT,
            None,
            submitted_at,
            None,
            conversation_id=None,
            tool="chat",
            status="pending",
        )
        get_r().hset(
            SUMMARY_PENDING,
            job_id,
            json.dumps({
                "conversation_id": conv_id,
                "through_seq": new_through_seq,
            }),
        )
        get_r().rpush(job_queue_for("chat"), json.dumps(envelope))
        log.info(
            "summary job enqueued",
            extra={
                "event": "summary_enqueued",
                "job_id": job_id,
                "conversation_id": conv_id,
                "through_seq": new_through_seq,
                "input_turns": len(summary_input) - 1,
            },
        )

    def _rebuild_messages_for_requeue(row) -> Optional[list[dict]]:
        """When a worker abandons or times out a job, the next worker needs
        the same chat envelope the original /generate produced. We rebuild
        it from the persisted conversation messages so requeued jobs still
        hit /api/chat instead of silently degrading to /api/generate.

        Returns ``None`` for jobs that were never part of a conversation
        (canaries, /generate calls with no ``conversation_id``) — those
        keep the legacy single-prompt path."""
        if row is None:
            return None
        keys = row.keys() if hasattr(row, "keys") else []
        if "conversation_id" not in keys:
            return None
        conversation_id = row["conversation_id"]
        if not conversation_id:
            return None
        all_msgs = db.list_messages(conversation_id)
        # The user message that triggered this job is the latest non-empty
        # user row; everything earlier than its assistant pair is the
        # history. Since the coordinator stores the user turn at enqueue
        # time, walking from the back picks it up reliably even when the
        # in-flight assistant row is still pending/error.
        new_user_text = row["prompt"] or ""
        prior: list = []
        found_match = False
        for m in all_msgs:
            if (
                not found_match
                and m["role"] == "user"
                and (m["text"] or "") == new_user_text
            ):
                found_match = True
                continue
            if found_match:
                continue
            prior.append(m)
        return _build_chat_messages(prior, new_user_text)

    def _job_row_to_envelope(row) -> dict:
        """Reconstruct the worker-facing job envelope from a jobs row.
        Used when the in-flight processing-hash entry is missing (claim
        raced with a reaper, or abandon arrived before claim).

        Reconstructed envelope must match the shape /generate pushes —
        no submitted_by_member_id (see the canary-detection comment in
        /generate), tool carried through so requeue lands on the right
        queue, and image_params restored to defaults for image jobs
        (per-job params aren't persisted; a requeue after timeout may
        therefore use defaults instead of the user's chosen width/steps —
        a deliberate KISS tradeoff)."""
        keys = row.keys() if hasattr(row, "keys") else []
        tool = row["tool"] if "tool" in keys else "chat"
        env: dict = {
            "job_id": row["job_id"],
            "prompt": row["prompt"],
            "model": row["model"],
            "submitted_at": row["submitted_at"],
            "tool": tool,
        }
        # Smart-routed chat is derived from the persisted model, the same
        # rule /generate applied — so a requeued smart job goes back to the
        # pipeline head's queue instead of a 3B chat worker.
        route = model_registry.route_for(tool, row["model"])
        if route != tool:
            env["route"] = route
        if tool in ("chat", "search"):
            msgs = _rebuild_messages_for_requeue(row)
            if msgs is not None:
                env["messages"] = msgs
        elif tool == "image":
            env["image"] = _default_image_params().model_dump()
        if tool == "search":
            # search_mode isn't persisted to the jobs row, so a requeue
            # after reaper timeout defaults to "fast". Same KISS tradeoff
            # image-param requeue makes.
            env["search"] = {"mode": "fast"}
        return env

    @router.get("/health")
    def health():
        try:
            get_r().ping()
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"redis unavailable: {e}")

    @router.get("/tos", response_class=HTMLResponse)
    def tos_html():
        """Public ToS page. Used both as the destination of the redemption-
        page link and as a stable URL contributors can revisit any time.
        The markdown body is fetched client-side from /tos/raw and rendered
        via marked.js so headings, lists, and emphasis come through as a
        real document, not preformatted ASCII in a <pre> block."""
        import html as html_lib
        return HTMLResponse(_TOS_HTML_TEMPLATE.substitute(
            version=html_lib.escape(tos_version),
        ))

    @router.get("/tos/raw", response_class=PlainTextResponse)
    def tos_raw():
        """Raw markdown for clients that prefer it (or for grep-friendly
        diffs between versions)."""
        return PlainTextResponse(
            load_tos_text_fn(),
            headers={"X-Tos-Version": tos_version},
        )

    @router.post("/generate", response_model=GenerateResponse)
    def generate(req: GenerateRequest, request: Request):
        r = get_r()
        if not req.prompt or not req.prompt.strip():
            raise HTTPException(status_code=400, detail="prompt required")
        if MAX_PROMPT_BYTES > 0 and len(req.prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"prompt exceeds MAX_PROMPT_BYTES ({MAX_PROMPT_BYTES})",
            )
        tool = (req.tool or "chat").lower()
        if tool not in ("chat", "image", "search", "tts"):
            raise HTTPException(
                status_code=400, detail=f"unknown tool: {tool!r}",
            )
        # messages[] is the stateless OpenAI-compatible path (see
        # coordinator/openai_compat.py) — chat-only, since image/search/tts
        # don't take a chat-style history.
        if req.messages and tool != "chat":
            raise HTTPException(
                status_code=400,
                detail="messages[] is only supported for tool=\"chat\"",
            )
        # search_mode is validated here so a typo from the UI fails fast
        # instead of leaking through to the agent (which would silently
        # default to "fast"). Only checked when the caller actually
        # selected search.
        search_mode: Optional[str] = None
        if tool == "search":
            search_mode = (req.search_mode or "fast").lower()
            if search_mode not in ("fast", "comprehensive"):
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown search_mode: {search_mode!r}",
                )

        # Default the model for image jobs when the caller didn't pick one.
        # Done before STRICT_MODELS validation so the registry check sees a
        # concrete name. Single source of truth: model_registry.DEFAULT_IMAGE_MODEL.
        if tool == "image" and not req.model:
            req.model = model_registry.DEFAULT_IMAGE_MODEL
        # Same shape for TTS — the v1 Piper voice is the default the
        # agent's bootstrap pulls, so naming it here keeps coordinator and
        # agent in sync. Voice mode on the client never picks a model
        # explicitly today.
        if tool == "tts" and not req.model:
            req.model = model_registry.DEFAULT_TTS_MODEL
        # Smart mode: the UI sends a boolean, not a model name. Resolve it
        # to the smart-tier default here so everything downstream (strict
        # validation, queue routing via model_registry.route_for, the
        # message rows' model stamp) sees a concrete model. An explicit
        # req.model wins — a caller pinning a smart-tier model directly
        # gets smart routing with or without the flag.
        if tool == "chat" and req.smart and not req.model:
            req.model = model_registry.DEFAULT_SMART_MODEL

        # optional model-registry validation (off unless STRICT_MODELS=true)
        try:
            model_registry.validate_or_raise(req.model, strict=STRICT_MODELS)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Cross-check: tool="chat" must not target an image model, and
        # vice versa. Catches paste-bomb mistakes (someone passing `sd1.5`
        # with tool="chat") regardless of STRICT_MODELS. Only enforced
        # when the model is in the registry — unknown names slip through
        # the same way validate_or_raise lets them through in lax mode.
        # Search jobs run on chat models (they post the search results to
        # the LLM as a system message), so the expected kind is "chat" for
        # both tool="chat" and tool="search".
        expected_kind = "chat" if tool in ("chat", "search") else tool
        if req.model and model_registry.is_known(req.model):
            m = model_registry.get(req.model)
            if m is not None and m.kind != expected_kind:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"model {req.model!r} is a {m.kind} model; "
                        f"submit with tool={m.kind!r} (got {tool!r})"
                    ),
                )

        # Refuse banned prompts for image jobs at submit time so a
        # contributor's machine never has to run them. See
        # _IMAGE_PROMPT_DENYLIST for what's covered.
        if tool == "image":
            blocked = _image_prompt_is_blocked(req.prompt)
            if blocked:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"image prompt refused: matches denylist term "
                        f"{blocked!r}. See /tos for the content policy."
                    ),
                )

        # optional retry-safety: same Idempotency-Key returns the same job_id.
        # The idempotent path returns BEFORE the live-worker check — the
        # original submission was already accepted, so we just hand the
        # caller back the same job_id to resume polling.
        idem_key = request.headers.get("idempotency-key")
        existing = idem.lookup(idem_key)
        if existing:
            log.info(
                "idempotent retry",
                extra={"event": "idempotent_hit", "job_id": existing},
            )
            existing_msg = db.get_message_by_job(existing)
            return GenerateResponse(
                job_id=existing,
                assistant_message_id=(
                    existing_msg["message_id"] if existing_msg is not None else None
                ),
            )

        # Refuse to accept the job if no worker advertising this tool has
        # heartbeated recently (REQUIRE_LIVE_WORKER=true on prod). Runs
        # after prompt/idempotency validation so 400s still win, and BEFORE
        # any DB writes so a 503 leaves no orphan job/message rows. Smart-
        # routed chat needs a pipeline head specifically — re-checked below
        # once the conversation's pinned model is resolved, since a pinned
        # smart model can flip the route after this first gate.
        ensure_live_worker_fn(tool=model_registry.route_for(tool, req.model))
        # Same placement rationale as the live-worker gate above: after
        # validation, before any DB writes, so a 503 here leaves no orphan
        # rows. Independent of REQUIRE_LIVE_WORKER — this protects fleet
        # capacity even when the live-worker gate is off.
        ensure_capacity_fn()

        member = getattr(request.state, "member", None)
        submitted_by = member.member_id if member is not None else None

        # Signup accounts with an unconfirmed email can't consume until
        # they click the link — see POST /signup / GET /verify-email.
        # Contributing (running the agent as a worker) is never gated by
        # this; only submitting a job (this endpoint) is.
        if member is not None and not member.email_verified:
            raise HTTPException(
                status_code=403,
                detail=(
                    "verify your email to unlock chat, image generation, "
                    "and voice — check your inbox, or POST "
                    "/me/resend-verification if it didn't arrive"
                ),
            )

        # Two-dimensional daily-quota enforcement (slice 2 + image-limits
        # slice). NULL on either column = unlimited for that dimension
        # (admin, tier-unlimited contributor). The check runs against
        # today's usage at submission time; a single prompt can overshoot
        # the chat cap by its completion size, which we don't predict here.
        # Image jobs gate on the image_units column instead — token output
        # for image jobs is unrelated to image-cost weighting.
        if member is not None:
            usage_today = db.member_usage_today(member.member_id)
            if tool == "image":
                cap = member.daily_quota_images
                if cap is not None and cap > 0:
                    used_units = usage_today["image_units"]
                    if used_units >= cap:
                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"daily image quota exceeded: "
                                f"{used_units:g} / {cap} image-units used today"
                            ),
                        )
            elif tool == "tts":
                # Voice cap precedence: explicit per-member override wins;
                # otherwise the tier's default voice_minutes from
                # tiers.TIER_QUOTAS. Different from tokens/images (where
                # NULL = unlimited) because voice ships with tier-driven
                # defaults — see voice-phase1 design memory. Admin is
                # always unlimited regardless of column value.
                if member.role != "admin":
                    cap = member.daily_quota_voice_minutes
                    if cap is None:
                        cap = _tier_quota_for(member.tier).get("voice_minutes")
                    if cap is not None and cap > 0:
                        used_min = usage_today["voice_seconds"] / 60.0
                        if used_min >= cap:
                            raise HTTPException(
                                status_code=429,
                                detail=(
                                    f"daily voice quota exceeded: "
                                    f"{used_min:.1f} / {cap} voice-minutes used today"
                                ),
                            )
            else:
                cap = member.daily_quota_tokens
                if cap is not None and cap > 0:
                    used = usage_today["tokens_out"]
                    if used >= cap:
                        raise HTTPException(
                            status_code=429,
                            detail=(
                                f"daily quota exceeded: {used} / "
                                f"{cap} output tokens used today"
                            ),
                        )

        # Conversation context: if the caller passed conversation_id, load
        # the prior turns and build a chat messages[] array for the worker
        # (Ollama /api/chat) so the model gets its own chat template applied
        # instead of plain-text autocompletion. Ownership is enforced — a
        # caller cannot inject into someone else's conversation.
        #
        # Image jobs skip the messages[] envelope entirely — sd.cpp takes
        # a single prompt string, not a chat history — but they DO live
        # inside conversations so a user's image generations show up
        # interleaved with chat in the sidebar.
        conversation_id: Optional[str] = req.conversation_id
        worker_messages: Optional[list[dict]] = None
        history_info: Optional[dict] = None
        if conversation_id:
            conv_row = db.get_conversation(conversation_id)
            if conv_row is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            _require_conversation_owner(request, conv_row)
            if conv_row["archived_at"] is not None:
                raise HTTPException(
                    status_code=410, detail="conversation is archived"
                )
            prior = db.list_messages(conversation_id)
            # Don't let a caller queue a new turn while a previous one is
            # still streaming — the conversation history would then contain
            # an empty/partial assistant turn wedged between two user turns,
            # which makes a mess of the worker-facing prompt and the UI.
            # The client UI also disables submit while pending, but the
            # server check is what makes the rule load-bearing.
            if prior and prior[-1]["status"] == "pending":
                raise HTTPException(
                    status_code=409,
                    detail="previous turn is still streaming",
                )
            if tool in ("chat", "search"):
                # Search jobs reuse the chat-style messages envelope so the
                # worker has the same conversation context to ground the
                # summary in (handy for follow-ups like "what about in
                # Europe?"). The worker prepends its own search-results
                # system message before calling Ollama.
                summary_text = (
                    conv_row["summary_text"]
                    if "summary_text" in conv_row.keys()
                    else None
                )
                summary_through_seq = (
                    conv_row["summary_through_seq"]
                    if "summary_through_seq" in conv_row.keys()
                    else None
                )
                # Attached-document context (chat only — see the doc-upload
                # scope note in coordinator/uploads.py; search jobs build
                # their own worker-side context and don't need this).
                document_context = (
                    uploads_lib.build_document_context(db.list_uploads(conversation_id))
                    if tool == "chat"
                    else None
                )
                worker_messages, history_info = _build_chat_messages_with_info(
                    prior, req.prompt,
                    summary_text=summary_text,
                    summary_through_seq=summary_through_seq,
                    document_context=document_context,
                )
                # Fire-and-forget summarization for any newly-dropped turns.
                # The summary job runs through the normal worker pool; its
                # completion replaces conversations.summary_text so the
                # NEXT /generate gets the benefit. The current turn pays
                # only the cap-truncated prefill cost (the summary it
                # eventually produces won't help this request).
                try:
                    _maybe_enqueue_summary_job(conv_row, prior, history_info)
                except Exception as e:
                    log.warning(
                        "summary enqueue failed (non-fatal): %s", e,
                        extra={"event": "summary_enqueue_failed"},
                    )
            # Conversation may pin a default model; honor it when the call
            # didn't override. For image jobs we DO NOT inherit a
            # chat-conversation's pinned LLM (that would re-trigger the
            # tool/model mismatch above) — only inherit when the pinned
            # model is in the same kind. Search and chat share the same
            # underlying model kind, so they can inherit from each other.
            if not req.model and conv_row["model"]:
                pinned = conv_row["model"]
                pinned_kind = (
                    model_registry.get(pinned).kind
                    if model_registry.is_known(pinned)
                    and model_registry.get(pinned) is not None
                    else "chat"
                )
                if pinned_kind == expected_kind:
                    req_model = pinned
                else:
                    req_model = model_registry.DEFAULT_IMAGE_MODEL if tool == "image" else None
            else:
                req_model = req.model
        elif req.messages:
            # Stateless OpenAI-compatible path (coordinator/openai_compat.py):
            # no conversation row to rebuild history from — the external
            # caller manages its own history and resends the full array each
            # call, so pass it straight through to the worker envelope.
            worker_messages = req.messages
            req_model = req.model
        else:
            req_model = req.model

        # Final routing key — req_model may differ from req.model after
        # conversation-pin inheritance (e.g. a smart-mode conversation's
        # follow-up turn arrives with no explicit model or flag). When the
        # route flipped to chat:smart only now, the earlier liveness gate
        # checked the wrong pool, so re-check before any DB writes.
        route = model_registry.route_for(tool, req_model)
        if route != model_registry.route_for(tool, req.model):
            ensure_live_worker_fn(tool=route)

        job_id = str(uuid.uuid4())
        submitted_at = time.time()
        # IMPORTANT: do NOT include submitted_by_member_id in the worker-
        # facing envelope. The worker has no need for it, and including
        # it lets a malicious worker recognize canaries (null submitter)
        # and selectively cheat on real prompts. Attribution lives on
        # the jobs DB row instead, which the coordinator reads directly
        # when crediting earnings / member_usage on /jobs/complete.
        job = {
            "job_id": job_id,
            "prompt": req.prompt,
            "model": req_model,
            "submitted_at": submitted_at,
            "tool": tool,
        }
        if route != tool:
            # Routing key for requeue paths that only have the envelope in
            # hand (reaper, abandon). tool stays "chat" so the agent's
            # chat handler — streaming, partials, token accounting — runs
            # unchanged; only the queue placement differs.
            job["route"] = route
        if worker_messages is not None:
            # The worker prefers messages[] (routed to Ollama /api/chat) when
            # present, falling back to the bare prompt for single-shot
            # generations and canaries. Keeping both fields keeps the
            # envelope backward-compatible with any worker that's still on
            # the old build.
            job["messages"] = worker_messages
        if tool == "chat" and req.voice_mode:
            # Tell the agent to pipeline first-sentence TTS in parallel with
            # LLM streaming. Only meaningful on chat; image/search/tts agents
            # ignore the field. Omitted (rather than set false) so a legacy
            # agent on an older build never sees an unknown key.
            job["voice_mode"] = True
        if tool == "image":
            # Image-only knobs. Only include fields the user explicitly
            # pinned so the worker can fall through to the model's sidecar
            # defaults (steps / sampler / cfg) for everything else. Hard
            # defaults here silently override LCM-tuned sidecars and cost
            # 3-4× per job — see the v1.1.24 fix.
            params = req.image or _default_image_params()
            image_env: dict = {
                "seed": params.seed,
                "negative_prompt": _combine_negative_prompt(params.negative_prompt),
            }
            # Clamp width/height to sane bounds (multiple of 64 in [256,
            # 1536]) so a malicious or buggy client can't ask the worker
            # to spend 10 minutes on an 8K image. sd.cpp itself also
            # requires multiples of 64.
            if params.width is not None:
                image_env["width"] = _clamp_image_dim(params.width)
            if params.height is not None:
                image_env["height"] = _clamp_image_dim(params.height)
            if params.steps is not None:
                image_env["steps"] = max(1, min(50, int(params.steps)))
            if params.init_image_b64:
                # Image alteration (img2img). Validates + NSFW-classifies
                # BEFORE this job ever reaches a queue — raises 4xx here,
                # same as every other pre-dispatch input check in this
                # handler, rather than letting a contributor's agent
                # discover the problem after doing the work.
                _validate_and_classify_init_image(params.init_image_b64, job_id)
                image_env["init_image_b64"] = params.init_image_b64
                if params.strength is not None:
                    # (0, 1] — 0 would mean "no change at all" (a wasted
                    # job); sd.exe's own default (0.75) applies when the
                    # client omits this entirely.
                    image_env["strength"] = min(1.0, max(0.01, float(params.strength)))
            job["image"] = image_env
        if tool == "search":
            # search_mode is validated above; carry it through so the agent
            # can branch fast (snippets) vs comprehensive (fetch + extract).
            job["search"] = {"mode": search_mode or "fast"}
        # Decide whether this job should go through the context-aware
        # rewrite pipeline. Two flavors share the same skip-paths:
        # - tool=image: rewrite the visual prompt using prior turns (see
        #   _enqueue_chat_rewrite_for_image)
        # - tool=search: rewrite the DDG query using prior turns (see
        #   _enqueue_chat_rewrite_for_search) — "try again" → "different
        #   recent news topic"
        #
        # Skipped when: chat tool (no rewrite needed), no conversation_id,
        # empty conversation (first turn — nothing to refine against), no
        # chat worker online (avoid stranding the job behind a rewrite
        # nobody can pick up).
        rewriteable = tool in ("image", "search")
        needs_rewrite = (
            rewriteable
            and conversation_id is not None
            and prior
            and _conversation_has_prior_context(prior)
            and chat_worker_available_for_rewrite_fn()
        )

        # Store the ORIGINAL user message (not the prepended worker-prompt)
        # so /jobs/complete can replay only the new turn into the
        # conversation history.
        db.insert_job(
            job_id,
            req.prompt,
            req_model,
            submitted_at,
            submitted_by,
            conversation_id=conversation_id,
            tool=tool,
            status=("awaiting_rewrite" if needs_rewrite else "pending"),
        )
        # Feeds the canary injector's traffic gate (see coordinator/canaries.py)
        # — counts real, customer-facing submissions only, not the hidden
        # rewrite/summary jobs this handler may also enqueue below.
        r.incr(CANARY_REAL_JOBS_SINCE)
        # Persist the user turn and an empty pending assistant turn now,
        # not at /jobs/complete time. This means: (a) a client that
        # disconnects mid-stream can reload /conversations and see its
        # message + the partial answer so far; (b) if the job fails, the
        # user's message stays visible with an error bubble in its place
        # (vs. the old behavior of erasing the user's prompt on failure).
        assistant_message_id: Optional[str] = None
        if conversation_id:
            base_seq = db.next_message_seq(conversation_id)
            user_msg_id = "msg_" + uuid.uuid4().hex[:12]
            assistant_message_id = "msg_" + uuid.uuid4().hex[:12]
            db.append_message(
                message_id=user_msg_id,
                conversation_id=conversation_id,
                seq=base_seq,
                role="user",
                text=req.prompt,
                model=req_model,
                created_at=submitted_at,
                status="complete",
            )
            db.append_message(
                message_id=assistant_message_id,
                conversation_id=conversation_id,
                seq=base_seq + 1,
                role="assistant",
                text="",
                job_id=job_id,
                model=req_model,
                created_at=submitted_at,
                status="pending",
            )
            db.touch_conversation(conversation_id, submitted_at)
            # First-prompt-becomes-the-title behavior is idempotent (set only
            # when title is NULL/empty) so it's safe to call here even
            # though the message is now persisted earlier than before.
            db.set_conversation_title(conversation_id, req.prompt[:80].strip())
        if needs_rewrite:
            # Hand the envelope to the matching rewrite pipeline (image or
            # search). The pipeline enqueues a hidden chat job; the real
            # job goes on its target queue later, in /jobs/complete's
            # rewrite-dispatch handler.
            if tool == "image":
                enqueue_chat_rewrite_for_image_fn(
                    image_job_id=job_id,
                    image_envelope=job,
                    original_prompt=req.prompt,
                    history=_format_rewrite_history(prior),
                    submitted_by=submitted_by,
                    submitted_at=submitted_at,
                )
            else:  # tool == "search"
                enqueue_chat_rewrite_for_search_fn(
                    search_job_id=job_id,
                    search_envelope=job,
                    original_prompt=req.prompt,
                    history=_format_rewrite_history(prior),
                    submitted_by=submitted_by,
                    submitted_at=submitted_at,
                )
        else:
            r.rpush(job_queue_for(route), json.dumps(job))
        idem.remember(idem_key, job_id)
        log.info(
            "queued job",
            extra={
                "event": "job_queued",
                "job_id": job_id,
            },
        )
        return GenerateResponse(
            job_id=job_id,
            assistant_message_id=assistant_message_id,
            history_info=history_info,
        )

    @router.get("/result/{job_id}")
    def result(job_id: str, request: Request):
        r = get_r()
        # Ownership gate: a member may only poll their own jobs; admin can
        # read any (moderation). Mirrors /jobs/cancel + /jobs/displayed.
        # No-op when AUTH is disabled (dev/test). job_id is a uuid4 so this
        # is defense-in-depth, but the control must not rely on entropy.
        if AUTH_ENABLED:
            member = getattr(request.state, "member", None)
            if member is None:
                raise HTTPException(status_code=401, detail="unauthorized")
            if member.role != "admin":
                owner_row = db.get_job(job_id)
                submitted_by = (
                    owner_row["submitted_by_member_id"]
                    if owner_row is not None
                    and "submitted_by_member_id" in owner_row.keys()
                    else None
                )
                if submitted_by is None or submitted_by != member.member_id:
                    raise HTTPException(status_code=404, detail="job not found")
        raw = r.hget(JOB_RESULTS, job_id)
        if raw:
            data = json.loads(raw)
            data.setdefault("status", "complete")
            data["done"] = data.get("status") in ("complete", "error")
            return data
        row = db.get_job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        submitted_by = (
            row["submitted_by_member_id"]
            if "submitted_by_member_id" in row.keys()
            else None
        )
        # Mid-stream: if the worker has been pushing partials, surface the
        # latest accumulated text so the polling client can render it.
        # status stays 'pending'/'running' so the client keeps polling.
        partial_text = r.hget(JOB_PARTIALS, job_id)
        # Voice-mode chat: the agent ships audio chunks on partials before
        # the LLM completes. Read the per-seq hash, sort by seq, and surface
        # the ordered list so the polling client can queue new chunks as
        # they arrive.
        audio_chunks_list: list[dict] = []
        chunks_raw = r.hgetall(f"{JOB_AUDIO_CHUNKS}:{job_id}")
        if chunks_raw:
            try:
                audio_chunks_list = [json.loads(v) for v in chunks_raw.values()]
                audio_chunks_list.sort(key=lambda c: int(c.get("seq", 0)))
            except (json.JSONDecodeError, ValueError):
                audio_chunks_list = []
        status = row["status"]
        # Image jobs persist their result as messages.image_path (not on
        # the jobs row). Look it up so the polling path can surface the
        # PNG URL after a JOB_RESULTS eviction.
        image_path: Optional[str] = None
        msg = db.get_message_by_job(job_id)
        if msg is not None and "image_path" in msg.keys():
            image_path = msg["image_path"]
        # Search jobs also carry a sources[] list when complete (lives only
        # on JOB_RESULTS — it's render-only data, not stored back to the
        # jobs row). On the DB-fallback path here there's no JOB_RESULTS
        # entry to read from, so sources end up null — that's fine because
        # the client uses the JOB_RESULTS path for fresh completions and
        # the DB-fallback path only fires after eviction.
        return {
            "job_id": row["job_id"],
            "status": status,
            "worker_id": row["worker_id"],
            "model": row["model"],
            "text": partial_text if partial_text is not None else row["result"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "earnings": row["earnings"],
            "duration_seconds": row["duration_seconds"],
            "attempts": row["attempts"],
            "error": row["error"],
            "submitted_by_member_id": submitted_by,
            "image_path": image_path,
            "sources": None,
            # search_was_skipped is a one-shot client signal and only
            # lives on JOB_RESULTS while the result is fresh. By the time
            # we're on this DB-fallback path (post-eviction), the client
            # has long since acted on it (or missed it). Defaulting to
            # null is harmless.
            "search_was_skipped": False,
            "audio_chunks": audio_chunks_list,
            "done": status in ("complete", "error"),
        }

    return router, generate, result, _job_row_to_envelope
