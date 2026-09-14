"""Small standalone endpoints with no other natural home: the model
catalog, earnings lookups, and the admin metrics snapshot.

``require_admin_fn`` is injected rather than imported because
``_require_admin`` still lives in coordinator/main.py (it moves to
coordinator/routes_observability.py in a later commit of this same
god-file split) — same closure-over-dependencies shape
coordinator/notifications.py already uses for ``db``. main.py wires it
once: ``app.include_router(routes_misc.build_router(db, r, require_admin_fn=_require_admin))``.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from coordinator import model_registry
from shared.config import JOB_PROCESSING, JOB_QUEUE, STRICT_MODELS, WORKER_TIMEOUT_SECONDS


def build_router(db, r, require_admin_fn) -> APIRouter:
    router = APIRouter()

    @router.get("/models")
    def models():
        """Catalog of models the coordinator knows about. See coordinator/model_registry.py."""
        return {
            "strict": STRICT_MODELS,
            "models": [m.to_dict() for m in model_registry.list_all()],
        }

    @router.get("/earnings")
    def earnings(request: Request):
        require_admin_fn(request)
        rows = db.list_earnings()
        workers_list = [
            {
                "worker_id": row["worker_id"],
                "total_tokens": int(row["total_tokens"]),
                "total_jobs": int(row["total_jobs"]),
                "total_usd": round(float(row["total_usd"]), 8),
            }
            for row in rows
        ]
        return {
            "workers": workers_list,
            "total_usd": round(sum(w["total_usd"] for w in workers_list), 8),
        }

    @router.get("/earnings/{worker_id}")
    def earnings_for(worker_id: str, request: Request):
        require_admin_fn(request)
        row = db.earnings_for(worker_id)
        if row is None:
            return {"worker_id": worker_id, "total_tokens": 0, "total_usd": 0.0}
        return {
            "worker_id": row["worker_id"],
            "total_tokens": int(row["total_tokens"]),
            "total_usd": round(float(row["total_usd"]), 8),
        }

    @router.get("/metrics")
    def metrics(request: Request):
        require_admin_fn(request)
        m = db.metrics()
        m["queue_depth"] = r.llen(JOB_QUEUE)
        m["processing"] = r.hlen(JOB_PROCESSING)
        now = time.time()
        workers_rows = db.list_workers()
        m["active_workers"] = sum(
            1 for w in workers_rows if (now - float(w["last_seen"] or 0)) < WORKER_TIMEOUT_SECONDS
        )
        m["registered_workers"] = len(workers_rows)
        return m

    return router
