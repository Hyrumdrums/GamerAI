"""``DB.metrics`` — coordinator/db.py god-file split. Mixin composition
(not delegation): every method keeps referencing ``self._conn``/
``self._lock`` exactly as before, since ``self`` still resolves to the
composed ``DB`` instance via MRO — a pure cut/paste, no call-site
changes needed anywhere else in the coordinator. See coordinator/db.py
for the full ``class DB(...)`` composition.
"""


class MetricsMixin:
    # ---------- metrics ----------
    def metrics(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS completed, "
                "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending, "
                "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
                "AVG(CASE WHEN status='complete' THEN duration_seconds END) AS avg_latency, "
                "COALESCE(SUM(completion_tokens), 0) AS tokens, "
                "COALESCE(SUM(earnings), 0) AS paid "
                "FROM jobs"
            )
            row = cur.fetchone()
        return {
            "total_jobs": row["total"] or 0,
            "completed_jobs": row["completed"] or 0,
            "failed_jobs": row["failed"] or 0,
            "pending_jobs": row["pending"] or 0,
            "running_jobs": row["running"] or 0,
            "avg_latency_seconds": round(row["avg_latency"], 4) if row["avg_latency"] else 0.0,
            "tokens_processed": int(row["tokens"] or 0),
            "total_paid_usd": round(float(row["paid"] or 0), 8),
        }
