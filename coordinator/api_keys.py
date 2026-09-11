"""Self-serve API keys — a member's own programmatic credential onto the
same contributor GPU network and per-member tier quota the web chat UI
already uses (see coordinator/openai_compat.py for the OpenAI-compatible
endpoint these keys are meant to be used against).

Lives in ``member_tokens`` (coordinator/db.py) alongside agent-pairing
tokens, distinguished by ``kind='api_key'`` and restricted via
``scope='generation'`` — enforced centrally in coordinator/main.py's
``_is_generation_scoped_allowed`` / ``_auth_middleware``, not here. This
module only handles the create/list/revoke lifecycle:

    POST   /me/api-keys                  member
    GET    /me/api-keys                  member
    POST   /me/api-keys/{prefix}/revoke  member

The router is built via a closure that captures ``db`` rather than
importing it from ``coordinator.main`` (circular) or instantiating a
fresh ``DB()`` per call (separate connection, bad) — same shape
coordinator/notifications.py and coordinator/uploads.py already use.
main.py wires it once: ``app.include_router(api_keys.build_router(db))``.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from coordinator import member_auth
from shared.auth import AUTH_ENABLED

# A member's live-key count is bounded so the table and the abuse surface
# stay small — this is a personal-use feature, not a multi-tenant API
# product. Raise it if a legitimate use case needs more.
MAX_API_KEYS_PER_MEMBER = 10


class ApiKeyCreateRequest(BaseModel):
    label: Optional[str] = None


def build_router(db) -> APIRouter:
    router = APIRouter()

    @router.post("/me/api-keys")
    def create_api_key(req: ApiKeyCreateRequest, request: Request):
        """Mint a new self-serve API key. The raw key is returned ONLY in
        this response — like any other API-key product, it's never
        persisted in plaintext and can never be shown again; a member who
        loses it has to revoke and mint a new one."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                raise HTTPException(
                    status_code=400,
                    detail="API keys require auth; set API_TOKEN first",
                )
            raise HTTPException(status_code=401, detail="unauthorized")
        existing = db.list_member_tokens(member.member_id, kind="api_key")
        if len(existing) >= MAX_API_KEYS_PER_MEMBER:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"maximum of {MAX_API_KEYS_PER_MEMBER} API keys per "
                    f"member — revoke one before creating another"
                ),
            )
        raw_token = member_auth.generate_token(prefix=member_auth.API_KEY_TOKEN_PREFIX)
        token_hash = member_auth.hash_token(raw_token)
        now = time.time()
        label = (req.label or "").strip() or None
        db.add_member_token(
            token_hash=token_hash,
            member_id=member.member_id,
            label=label,
            when=now,
            kind="api_key",
            scope="generation",
        )
        return {
            "api_key": raw_token,
            "id": token_hash[:12],
            "label": label,
            "created_at": now,
        }

    @router.get("/me/api-keys")
    def list_api_keys(request: Request):
        """Never reveals the raw bearer (we don't have it — we only
        stored the hash); the id prefix is enough to disambiguate rows in
        the UI and is what the revoke POST takes as a slug — same
        convention as GET /me/machines."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"api_keys": []}
            raise HTTPException(status_code=401, detail="unauthorized")
        rows = db.list_member_tokens(member.member_id, kind="api_key")
        return {
            "api_keys": [
                {
                    "id": row["token_hash"][:12],
                    "label": row["label"],
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"],
                }
                for row in rows
            ]
        }

    @router.post("/me/api-keys/{prefix}/revoke")
    def revoke_api_key(prefix: str, request: Request):
        """Caller can only revoke keys they own — the lookup is scoped to
        the caller's member_id, so a prefix that matches another
        member's row 404s rather than leaking that the row exists."""
        member = getattr(request.state, "member", None)
        if member is None:
            if not AUTH_ENABLED:
                return {"deleted": False}
            raise HTTPException(status_code=401, detail="unauthorized")
        token_hash = db.resolve_member_token_hash(member.member_id, prefix, kind="api_key")
        if token_hash is None:
            raise HTTPException(status_code=404, detail="API key not found")
        deleted = db.delete_member_token(member.member_id, token_hash, kind="api_key")
        return {"deleted": deleted}

    return router
