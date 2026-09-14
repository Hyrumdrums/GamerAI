"""Admin-only debug/roster/test-email endpoints. ``db`` is closed over
directly; ``r`` as ``get_r`` (a zero-arg getter — see
coordinator/prompt_rewrite.py for why); ``require_admin_fn`` because
``_require_admin`` still lives in coordinator/main.py's not-yet-
extracted observability section (same shape coordinator/routes_misc.py
already uses). main.py wires it once:
``app.include_router(routes_admin.build_router(db, lambda: r, require_admin_fn=_require_admin))``.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from coordinator import email_send
from coordinator.prompt_rewrite import _parse_search_rewrite_output
from shared.auth import AUTH_ENABLED
from shared.config import IMAGE_REWRITE_PENDING, JOB_RESULTS, SEARCH_REWRITE_PENDING
from shared.models import TestEmailRequest


def build_router(db, get_r, require_admin_fn) -> APIRouter:
    router = APIRouter()

    @router.get("/admin/debug/job/{job_id}")
    def admin_debug_job(job_id: str, request: Request):
        """Admin-only: dump everything we know about a job's pipeline
        state, especially for search-rewrite debugging. Returns:

        - The DB job row (prompt, status, tool, result, etc.)
        - For a search job: any associated rewrite chat job — its own DB
          row + the parser decision (re-parsed from the stored output so
          we don't have to ship the dispatcher's runtime decision through
          another channel).
        - JOB_RESULTS payload (sources, search_was_skipped, image_path)
        - Any live pending-rewrite linkage (still in flight)
        - The Redis claim / processing state

        Built to make ANOTHER debug cycle for search regressions take
        minutes instead of the SSH-and-dump-SQLite dance that diagnosed
        the prior Kevin-O'Leary panic-recant bug."""
        member = getattr(request.state, "member", None)
        if AUTH_ENABLED and (member is None or member.role != "admin"):
            raise HTTPException(status_code=403, detail="admin only")

        job_row = db.get_job(job_id)
        if job_row is None:
            raise HTTPException(status_code=404, detail="job not found")

        def _row_to_dict(row):
            if row is None:
                return None
            return {k: row[k] for k in row.keys()}

        job_dict = _row_to_dict(job_row)

        r = get_r()
        # JOB_RESULTS — the final Redis payload the polling client reads.
        results_raw = r.hget(JOB_RESULTS, job_id)
        results_payload = None
        if results_raw:
            try:
                results_payload = json.loads(results_raw)
            except json.JSONDecodeError:
                results_payload = {"_raw": results_raw}

        # Find the rewrite chat job linked to this search/image, if any.
        # The linkage hash stores rewrite_job_id → {search_job_id|image_
        # job_id, envelope, original}; we scan for an entry where the
        # target job_id matches. Small hash (a few entries at most), so
        # O(n) is fine.
        pending_rewrite_link = None
        for hash_key in (SEARCH_REWRITE_PENDING, IMAGE_REWRITE_PENDING):
            for rid, link_raw in (r.hgetall(hash_key) or {}).items():
                try:
                    link = json.loads(link_raw)
                except json.JSONDecodeError:
                    continue
                target_id = link.get("search_job_id") or link.get("image_job_id")
                if target_id == job_id:
                    pending_rewrite_link = {
                        "kind": hash_key,
                        "rewrite_job_id": rid,
                        "original_prompt": link.get("original_prompt"),
                    }
                    break
            if pending_rewrite_link:
                break

        # Find the COMPLETED rewrite chat job that produced this search
        # job's current prompt. We don't store the linkage long-term
        # (it's deleted from SEARCH_REWRITE_PENDING after dispatch), so
        # this is a best-effort search through recent chat jobs that
        # have this job's conversation context. For now we just look up
        # the immediate prior rewrite-shaped chat job from the same
        # submission window.
        rewrite_job_dict = None
        rewrite_parsed = None
        if job_row["tool"] == "search":
            # Heuristic: a rewrite chat job submitted within 2s of this
            # search by the same submitter, with the meta-prompt
            # signature in its prompt field. Cheap and deterministic
            # for the recent-history case the user actually wants to
            # debug. We dip directly into the DB connection since this
            # query exists only for the debug surface; not worth a
            # promoted db.* method.
            rows = db._conn.execute(
                "SELECT * FROM jobs WHERE submitted_by_member_id IS ? "
                "AND tool = 'chat' AND submitted_at BETWEEN ? AND ? "
                "ORDER BY submitted_at DESC LIMIT 8",
                (
                    job_row["submitted_by_member_id"],
                    job_row["submitted_at"] - 2.0,
                    job_row["submitted_at"] + 2.0,
                ),
            ).fetchall()
            for c in rows:
                prompt = c["prompt"] or ""
                if "routing classifier" in prompt:
                    rewrite_job_dict = _row_to_dict(c)
                    rewrite_parsed = _parse_search_rewrite_output(c["result"])
                    break

        return {
            "job": job_dict,
            "job_results": results_payload,
            "pending_rewrite_link": pending_rewrite_link,
            "rewrite_job": rewrite_job_dict,
            "rewrite_parsed": (
                {"decision": rewrite_parsed[0], "value": rewrite_parsed[1]}
                if rewrite_parsed else None
            ),
        }

    @router.get("/admin/members")
    def admin_list_members(request: Request):
        """Admin-only roster. Returns enough to manage the network: id,
        role, tier, email, parent, quota, revoked-flag, last-active. Never
        returns the raw token (it isn't stored)."""
        member = getattr(request.state, "member", None)
        if AUTH_ENABLED and (member is None or member.role != "admin"):
            raise HTTPException(status_code=403, detail="admin only")
        rows = db.list_members()
        return {
            "members": [
                {
                    "member_id": r["member_id"],
                    "email": r["email"],
                    "role": r["role"],
                    "tier": r["tier"],
                    "parent_member_id": r["parent_member_id"],
                    "daily_quota_tokens": r["daily_quota_tokens"],
                    "revoked_at": r["revoked_at"],
                    "created_at": r["created_at"],
                    "last_active_at": r["last_active_at"],
                    "tos_accepted_at": r["tos_accepted_at"] if "tos_accepted_at" in r.keys() else None,
                    "tos_version": r["tos_version"] if "tos_version" in r.keys() else None,
                }
                for r in rows
            ]
        }

    @router.post("/admin/test-email")
    def admin_test_email(req: TestEmailRequest, request: Request):
        """Admin dashboard's "Email delivery test" card — sends a plain,
        unmistakably-a-test message (not the verification template) to a
        typed-in address so the admin can confirm Resend is actually
        delivering (DNS/domain verification, RESEND_API_KEY, etc.) without
        creating a throwaway signup just to trigger an email."""
        require_admin_fn(request)
        to = (req.to or "").strip()
        if not to or "@" not in to:
            raise HTTPException(status_code=400, detail="a valid email is required")
        if not email_send.is_configured():
            raise HTTPException(
                status_code=400,
                detail="RESEND_API_KEY is not configured on this coordinator",
            )
        ok, detail = email_send.send_test_email(to)
        if not ok:
            raise HTTPException(status_code=502, detail=f"send failed: {detail}")
        return {"sent": True, "to": to}

    return router
