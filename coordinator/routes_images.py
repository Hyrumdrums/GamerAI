"""Generated-image serving. ``db`` is captured via a closure — same
shape coordinator/notifications.py and coordinator/uploads.py already
use — since ``IMAGE_DIR`` (coordinator/image_moderation.py) is a plain,
unchanging import and there's no ``r``/test-reassignment hazard here.
main.py wires it once: ``app.include_router(routes_images.build_router(db))``.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from coordinator.image_moderation import IMAGE_DIR
from shared.auth import AUTH_ENABLED


def build_router(db) -> APIRouter:
    router = APIRouter()

    @router.get("/images/{name}")
    def serve_image(name: str, request: Request):
        """Serve a generated PNG. Ownership-checked: an authenticated caller
        can only fetch images attached to a conversation they own (admin
        bypass for moderation). Auth-off dev mode serves everything so
        local development with no API_TOKEN still works.

        The filename is the basename stored in messages.image_path
        ({job_id}.png). We resolve the job → conversation → owner chain
        on every request rather than baking a token into the URL so
        revocation is automatic when a member is removed."""
        # Path-traversal defense — filename must be plain.
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name or ""):
            raise HTTPException(status_code=400, detail="bad image name")
        path = IMAGE_DIR / name
        if not path.exists():
            raise HTTPException(status_code=404, detail="image not found")

        if AUTH_ENABLED:
            member = getattr(request.state, "member", None)
            if member is None:
                raise HTTPException(status_code=401, detail="unauthorized")
            if member.role != "admin":
                # Look up the job and walk to the conversation owner.
                job_id = name[:-4]  # strip .png
                job_row = db.get_job(job_id)
                if job_row is None:
                    raise HTTPException(status_code=404, detail="image not found")
                conv_id = (
                    job_row["conversation_id"]
                    if "conversation_id" in job_row.keys()
                    else None
                )
                if conv_id is None:
                    # Orphan image — no conversation to gate on. Refuse
                    # rather than leak; the only path that creates an
                    # orphan today is canary, which shouldn't produce
                    # images.
                    raise HTTPException(status_code=404, detail="image not found")
                conv_row = db.get_conversation(conv_id)
                if conv_row is None or conv_row["owner_member_id"] != member.member_id:
                    raise HTTPException(status_code=404, detail="image not found")
        return FileResponse(str(path), media_type="image/png")

    return router
