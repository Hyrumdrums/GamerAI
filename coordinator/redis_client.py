import os
import redis

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

JOB_QUEUE = "job_queue"
JOB_RESULTS = "job_results"
WORKER_REGISTRY = "worker_registry"
WORKER_EARNINGS = "worker_earnings"
WORKER_HEARTBEATS = "worker_heartbeats"


def get_client() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )
