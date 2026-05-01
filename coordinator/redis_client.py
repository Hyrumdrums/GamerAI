"""Thin Redis factory. Keys live in shared.config."""
import redis

from shared.config import redis_kwargs

# re-export for callers that imported from here previously
from shared.config import (  # noqa: F401
    JOB_QUEUE,
    JOB_RESULTS,
    JOB_PROCESSING,
    WORKER_REGISTRY,
    WORKER_HEARTBEATS,
    WORKER_STATUS,
    WORKER_EARNINGS,
)


def get_client() -> redis.Redis:
    return redis.Redis(decode_responses=True, **redis_kwargs())
