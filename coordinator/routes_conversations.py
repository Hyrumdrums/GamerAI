"""Conversation CRUD, listing, detail, and the assistant-message retry
flow. ``db`` is closed over directly; ``r`` as ``get_r`` (a zero-arg
getter — see coordinator/prompt_rewrite.py for why); ``ensure_live_worker_fn``
/ ``build_chat_messages_with_info_fn`` because ``_ensure_live_worker_or_503``
/ ``_build_chat_messages_with_info`` still live in coordinator/main.py's
not-yet-extracted generate()/public-API section (retry_message reuses
them verbatim rather than duplicating). main.py wires it once:
``app.include_router(routes_conversations.build_router(db, lambda: r, _ensure_live_worker_or_503, _build_chat_messages_with_info))``.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from coordinator import model_registry
from coordinator import uploads as uploads_lib
from coordinator.image_moderation import IMAGE_DIR
from shared.auth import AUTH_ENABLED
from shared.config import (
    CANARY_REAL_JOBS_SINCE,
    JOB_AUDIO_CHUNKS,
    JOB_PARTIALS,
    JOB_PROCESSING,
    JOB_RESULTS,
    job_queue_for,
)
from shared.models import ConversationCreateRequest

log = logging.getLogger("coordinator.routes_conversations")

# Minimum seconds between retry button presses for the same message.
# Enforced via Redis with a per-message TTL key so a client that
# bypasses the disabled button still gets rejected.
RETRY_COOLDOWN_SECONDS = 10


def _conv_row_to_summary(row) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "owner_member_id": row["owner_member_id"],
    }


def _message_row_to_dict(row) -> dict:
    keys = row.keys()
    return {
        "message_id": row["message_id"],
        "seq": row["seq"],
        "role": row["role"],
        "text": row["text"],
        "status": row["status"] if "status" in keys else "complete",
        "job_id": row["job_id"],
        "model": row["model"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "created_at": row["created_at"],
        "image_path": row["image_path"] if "image_path" in keys else None,
    }


def _require_conversation_owner(request: Request, conv_row) -> None:
    """Reject if the caller is authenticated and the conversation has
    an owner that isn't them. Auth-off mode permits everything (dev/test).
    """
    if not AUTH_ENABLED:
        return
    member = getattr(request.state, "member", None)
    if member is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    owner = conv_row["owner_member_id"]
    # Admin can read any conversation for moderation; otherwise strict
    # member_id match. (When prompt-encryption-at-rest ships, even the
    # admin won't be able to decrypt; for now this is honest.)
    if owner is not None and owner != member.member_id and member.role != "admin":
        raise HTTPException(status_code=404, detail="conversation not found")


def build_router(db, get_r, ensure_live_worker_fn, build_chat_messages_with_info_fn) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations")
    def create_conversation(req: ConversationCreateRequest, request: Request):
        """Create a new conversation owned by the caller. When auth is off
        (dev mode), owner_member_id is left NULL."""
        member = getattr(request.state, "member", None)
        owner_id = member.member_id if member is not None else None
        conv_id = "conv_" + uuid.uuid4().hex[:12]
        db.create_conversation(
            conversation_id=conv_id,
            owner_member_id=owner_id,
            title=req.title,
            model=req.model,
        )
        log.info(
            "conversation created",
            extra={"event": "conversation_created"},
        )
        return {"conversation_id": conv_id, "title": req.title, "model": req.model}

    @router.get("/conversations")
    def list_conversations(request: Request, include_archived: bool = False):
        """List the caller's conversations, most-recently-updated first.
        Admin gets back only their OWN conversations here, not the whole
        table — admin moderation of others would go through a separate
        /admin/conversations endpoint (not built yet)."""
        member = getattr(request.state, "member", None)
        if AUTH_ENABLED and member is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        owner_id = member.member_id if member is not None else None
        if owner_id is None:
            # Auth-off dev mode: conversations get owner_member_id=NULL when
            # there's no authenticated caller, so a member-id filter would
            # always miss. Surface those rows so the sidebar isn't empty.
            if not AUTH_ENABLED:
                rows = db.list_unowned_conversations(
                    include_archived=include_archived,
                )
                return {"conversations": [_conv_row_to_summary(r) for r in rows]}
            return {"conversations": []}
        rows = db.list_conversations_for_member(
            owner_id, include_archived=include_archived,
        )
        return {"conversations": [_conv_row_to_summary(r) for r in rows]}

    @router.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, request: Request):
        row = db.get_conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, row)
        messages = db.list_messages(conversation_id)
        # Stash the latest summary on the detail response so the client's
        # collapsible stats panel can render it without an extra round-trip.
        # Deliberately not on the list endpoint — the per-row payload would
        # balloon with thousand-char summaries no sidebar entry uses.
        keys = row.keys()
        summary_text = row["summary_text"] if "summary_text" in keys else None
        summary_through_seq = (
            row["summary_through_seq"] if "summary_through_seq" in keys else None
        )
        return {
            **_conv_row_to_summary(row),
            "summary_text": summary_text,
            "summary_through_seq": summary_through_seq,
            "messages": [_message_row_to_dict(m) for m in messages],
        }

    @router.post("/messages/{message_id}/retry")
    def retry_message(message_id: str, request: Request):
        """Re-enqueue a failed assistant message. Caller must own the
        conversation. Cooldown is server-enforced — the client UI disables
        its retry button for the same window but a hand-crafted request
        will still 429."""
        r = get_r()
        msg = db.get_message(message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="message not found")
        if msg["role"] != "assistant":
            raise HTTPException(status_code=400, detail="not an assistant message")
        conv_row = db.get_conversation(msg["conversation_id"])
        if conv_row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, conv_row)

        # Refuse to re-enqueue when no worker has heartbeated recently —
        # same policy as /generate. Runs BEFORE the cooldown gate so a
        # 503 doesn't burn the user's retry window.
        ensure_live_worker_fn()

        # Cooldown gate runs BEFORE the state check so a rapid double-click
        # gets 429'd (the spam signal) rather than 409'd (the "already
        # retrying" signal). SET NX EX is the atomic primitive here — only
        # the first caller in the window gets the OK, everyone else sees
        # the remaining TTL and gets 429'd.
        cd_key = f"retry_cd:{message_id}"
        if not r.set(cd_key, "1", nx=True, ex=RETRY_COOLDOWN_SECONDS):
            remaining = r.ttl(cd_key)
            retry_after = max(1, int(remaining)) if remaining is not None else RETRY_COOLDOWN_SECONDS
            raise HTTPException(
                status_code=429,
                detail=f"retry cooldown active; try again in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )

        if msg["status"] != "error":
            # Release the cooldown we just claimed since we're not actually
            # going to do work — otherwise an accidental click on a
            # complete/pending message would lock out a legitimate retry
            # on the same id later.
            r.delete(cd_key)
            raise HTTPException(
                status_code=409,
                detail=f"message is not in error state (status={msg['status']})",
            )

        # Rebuild the worker-facing prompt from prior turns. The user
        # message that produced this failure is at seq - 1; everything
        # before it is conversation context. We also pin the conversation
        # owner as the submitter for usage/quota accounting on retry.
        all_messages = db.list_messages(msg["conversation_id"])
        user_msg = None
        prior: list = []
        for m in all_messages:
            if m["message_id"] == message_id:
                break
            if m["seq"] == msg["seq"] - 1 and m["role"] == "user":
                user_msg = m
            else:
                prior.append(m)
        if user_msg is None:
            raise HTTPException(
                status_code=500,
                detail="cannot find the user message that produced this failure",
            )
        # Retries stay consistent with a normal /generate call: an
        # attached document doesn't silently drop out of context just
        # because this particular turn failed and got retried.
        document_context = uploads_lib.build_document_context(
            db.list_uploads(msg["conversation_id"])
        )
        worker_messages, _retry_history_info = build_chat_messages_with_info_fn(
            prior, user_msg["text"] or "", document_context=document_context,
        )

        # Pick a model: explicit conversation default → original message
        # model → coordinator default at job-fetch time. Keeping the same
        # model on retry avoids a surprise model swap mid-conversation.
        use_model = conv_row["model"] or msg["model"]
        # Carry the original job's tool forward so the retry lands on the
        # matching per-tool queue. Today only chat reaches this endpoint
        # (image/search messages don't surface retry buttons), but
        # defaulting to "chat" with a DB-driven lookup means an image
        # retry added later doesn't silently land on the chat queue.
        orig_tool = "chat"
        # sqlite3.Row exposes columns via __getitem__, not .get() — wrap
        # the lookup in try/except so a row without a job_id (legacy
        # message rows) falls through to the chat default cleanly.
        try:
            orig_job_id = msg["job_id"]
        except (IndexError, KeyError):
            orig_job_id = None
        if orig_job_id:
            orig_job = db.get_job(orig_job_id)
            if orig_job is not None:
                try:
                    orig_tool = (orig_job["tool"] or "chat").lower()
                except (IndexError, KeyError):
                    pass
        new_job_id = str(uuid.uuid4())
        submitted_at = time.time()
        member = getattr(request.state, "member", None)
        submitted_by = member.member_id if member is not None else None
        db.insert_job(
            new_job_id,
            user_msg["text"] or "",
            use_model,
            submitted_at,
            submitted_by,
            conversation_id=msg["conversation_id"],
        )
        if not db.reset_message_for_retry(message_id, new_job_id):
            # Someone else flipped status between get_message and now —
            # rare but possible. Release the cooldown and surface a 409.
            r.delete(cd_key)
            raise HTTPException(status_code=409, detail="message no longer in error state")
        # Feeds the canary injector's traffic gate — see coordinator/canaries.py.
        r.incr(CANARY_REAL_JOBS_SINCE)
        db.touch_conversation(msg["conversation_id"], submitted_at)
        job = {
            "job_id": new_job_id,
            "prompt": user_msg["text"] or "",
            "model": use_model,
            "submitted_at": submitted_at,
            "messages": worker_messages,
            "tool": orig_tool,
        }
        # A retried smart-mode turn keeps its model, so it must also keep
        # its queue — same model-derived rule as /generate and the reaper.
        retry_route = model_registry.route_for(orig_tool, use_model)
        if retry_route != orig_tool:
            job["route"] = retry_route
        r.rpush(job_queue_for(retry_route), json.dumps(job))
        log.info(
            "retry queued",
            extra={
                "event": "retry_queued",
                "job_id": new_job_id,
                "message_id": message_id,
            },
        )
        return {
            "ok": True,
            "job_id": new_job_id,
            "message_id": message_id,
            "cooldown_seconds": RETRY_COOLDOWN_SECONDS,
        }

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, request: Request):
        """Hard-delete the conversation and everything attached:

        - all message rows
        - all job rows linked via ``messages.job_id`` or ``jobs.conversation_id``
        - every generated PNG referenced by those messages (off disk)
        - residual Redis state for those jobs (JOB_RESULTS / JOB_PROCESSING / JOB_PARTIALS)
        - the conversation row itself

        A request to delete an unknown conversation returns 404; the
        member must own the conversation (admin can override) or they
        get the same 404 (we don't distinguish "yours doesn't exist" from
        "someone else's exists", per _require_conversation_owner).

        This used to be a soft archive (sets ``archived_at``). The
        contract changed in v1.1.26 once the UI grew a real delete button
        — users expect "delete" to mean "the bytes are gone," not "hidden
        from my sidebar." There is no current consumer of the archive
        semantics; if one shows up later, the path forward is a separate
        /conversations/{id}/archive endpoint rather than reviving this
        one."""
        r = get_r()
        row = db.get_conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, row)
        image_basenames, job_ids = db.purge_conversation(conversation_id)
        # Filesystem cleanup — best-effort per file; one missing or
        # locked file shouldn't block the deletion of the others.
        for name in image_basenames:
            # Defense in depth: the basename came from our own DB write
            # (a uuid + ".png"), but path-traversal validation here costs
            # nothing and protects against a future code path that stores
            # an attacker-controlled string in image_path.
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name or ""):
                continue
            path = IMAGE_DIR / name
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning(
                    "image file unlink failed",
                    extra={
                        "event": "image_unlink_failed",
                        "conversation_id": conversation_id,
                        "image_path": name,
                        "error": str(exc),
                    },
                )
        # Redis cleanup. Late /jobs/complete from a worker after deletion
        # will already 410 via the claim-token gate (the JOB_PROCESSING
        # entry is gone), so this is just sweeping up the result + partial
        # text that would otherwise linger until natural eviction.
        for jid in job_ids:
            r.hdel(JOB_RESULTS, jid)
            r.hdel(JOB_PROCESSING, jid)
            r.hdel(JOB_PARTIALS, jid)
            r.delete(f"{JOB_AUDIO_CHUNKS}:{jid}")
        log.info(
            "conversation deleted",
            extra={
                "event": "conversation_deleted",
                "conversation_id": conversation_id,
                "images_removed": len(image_basenames),
                "jobs_removed": len(job_ids),
            },
        )
        return {
            "ok": True,
            "deleted": True,
            "images_removed": len(image_basenames),
            "jobs_removed": len(job_ids),
        }

    return router
