"""Shared pydantic schemas for the coordinator API."""
from typing import List, Optional

from pydantic import BaseModel, Field


class ImageParams(BaseModel):
    """Optional per-image-job knobs.

    All fields are Optional so the worker's per-model sidecar defaults
    (steps / cfg / sampler / size) can win when the UI doesn't pin a
    value. A hard default here (the old ``steps: int = 20``) silently
    overrode the sidecar's LCM-tuned ``default_steps=8`` and made every
    image job take ~3-4× longer than it needed to. Only fields the user
    explicitly pinned should land in the job envelope; the rest fall
    through to the agent's ``load_sd_model_defaults`` lookup."""
    width: Optional[int] = None
    height: Optional[int] = None
    steps: Optional[int] = None
    seed: Optional[int] = None
    negative_prompt: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    # "chat" (default, legacy), "image", or "search". Each tool routes
    # to its own Redis queue and is only picked up by workers that
    # advertise the matching capability — chat-only agents never get
    # handed an image or search job.
    tool: str = "chat"
    # Image-only knobs. Ignored for tool="chat" / "search". When
    # tool="image" and omitted, the model's defaults are used.
    image: Optional[ImageParams] = None
    # Search-only knob. "fast" = DDG snippets only (~1s). "comprehensive"
    # = snippets + fetch & extract the top 5 result pages (~5-8s).
    # Ignored unless tool=="search". Defaults to fast on the worker
    # side if unset, so a misconfigured caller still gets a result.
    search_mode: Optional[str] = None
    # When set, the coordinator loads the prior turns of this
    # conversation, prepends them to the worker-facing prompt, and
    # auto-appends the user message + assistant response back into
    # the conversation on completion. Requires ownership: the caller's
    # member_id must match the conversation's owner_member_id.
    conversation_id: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str
    # When the request carries a conversation_id, the coordinator
    # persists a placeholder assistant message at enqueue time so a
    # client that disconnects mid-stream can find and resume it on
    # reload. The id is returned here so the active client can also
    # track the in-progress bubble without re-fetching the conversation.
    assistant_message_id: Optional[str] = None


class JobPartialRequest(BaseModel):
    """Worker push during streaming. ``text`` is the full accumulated
    output so far (not a delta), so the coordinator can do a simple
    replacement write. A retried worker's first partial therefore
    correctly overwrites any leftover text from the original attempt.

    ``claim_token`` is the per-claim secret issued by /jobs/next (or
    /jobs/claim). Coordinator rejects partials whose token doesn't
    match the current in-flight claim — the only path that lets a
    stale worker overwrite a re-dispensed job."""
    worker_id: str
    job_id: str
    text: str
    claim_token: Optional[str] = None


class WorkerCapabilities(BaseModel):
    """What a worker advertises it can run.

    All fields are optional so a legacy ``/register`` call carrying just a
    ``worker_id`` keeps working — capabilities are additive.
    """
    vram_gb: Optional[float] = None
    gpu_model: Optional[str] = None       # e.g. "RTX 4090"
    bandwidth_class: Optional[str] = None  # "low" | "medium" | "high"
    models: List[str] = Field(default_factory=list)
    # Per-tool capability. Defaults to ["chat"] for legacy agents that
    # don't include the field. Image-capable agents send
    # ["chat","image"] (or just ["image"] on a box too small for chat).
    # Coordinator uses this to route /jobs/next requests and to refuse
    # /generate when no worker advertising the requested tool is online.
    tools: List[str] = Field(default_factory=lambda: ["chat"])
    notes: Optional[str] = None


class WorkerIdent(BaseModel):
    worker_id: str
    capabilities: Optional[WorkerCapabilities] = None


class HeartbeatRequest(BaseModel):
    worker_id: str
    status: str = Field(default="idle")  # idle | busy | offline
    # When the worker is mid-job, the job_id it claims to be processing.
    # The reaper uses this to distinguish "worker silent" (requeue) from
    # "worker still alive and on the right job" (extend the deadline),
    # so a long image generation isn't falsely yanked out from under a
    # healthy worker.
    job_id: Optional[str] = None


class JobClaimRequest(BaseModel):
    worker_id: str
    job_id: str
    # Echo of the per-claim token (only checked by /jobs/abandon — the
    # other claim endpoints have dedicated request types). Optional so
    # /jobs/claim itself, which mints the token, can keep using this
    # type.
    claim_token: Optional[str] = None


class JobNextRequest(BaseModel):
    """Worker pulls the next job from a queue.

    Three call shapes, in order of preference:

    1. ``tools=[...] + wait=N`` — long-poll over multiple per-tool
       queues for up to N seconds via Redis BLPOP. The coordinator
       returns the first job to land on any of them, or null after
       the timeout. This is what v1.1.25+ agents use; it drops the
       job-dispatch latency from 0-5s to ~10ms because the worker is
       already blocking on Redis when the job is enqueued.
    2. ``tool=X`` (single) — legacy zero-wait LPOP on one queue.
       Pre-v1.1.25 agents and the in-VPS mock worker still send this
       shape; it stays supported so we don't have to flag-day the
       protocol.
    3. ``tools=[...]`` with ``wait=0`` — multi-queue immediate try,
       picks from each queue in list order until one is non-empty.
       Useful for testing.
    """
    worker_id: str
    tool: str = "chat"
    tools: Optional[List[str]] = None
    wait: float = 0.0


class JobCompleteRequest(BaseModel):
    worker_id: str
    job_id: str
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_seconds: float = 0.0
    status: str = "complete"  # complete | error
    error: Optional[str] = None
    # For image jobs: the generated PNG as base64. Coordinator decodes,
    # validates the PNG header, writes to /data/images/<job_id>.png,
    # and stamps the message's image_path. Empty/absent on chat jobs.
    image_b64: Optional[str] = None
    # For search jobs: ordered list of citations matching the [1][2]
    # references in ``text``. Each entry is {"n": int, "title": str,
    # "url": str, "domain": str}. Coordinator stores these on
    # JOB_RESULTS so /result/{job_id} can surface them to the client
    # for the end-of-bubble sources strip. Empty/absent on chat/image.
    sources: Optional[List[dict]] = None
    # Per-claim secret issued at job pickup. Coordinator returns 410
    # when this doesn't match the current claim — the case where the
    # reaper has already requeued the job and another worker is on
    # it. Optional in the schema so legacy clients fail with a clear
    # 410 rather than a 422 validation error.
    claim_token: Optional[str] = None


class InviteCreateRequest(BaseModel):
    """Contributor (or admin) creates an invite for an outside person.

    All fields optional. ``daily_quota_tokens`` defaults to NULL =
    unlimited; ``expires_hours`` defaults to no expiry.
    """
    daily_quota_tokens: Optional[int] = None
    invitee_email: Optional[str] = None
    expires_hours: Optional[float] = None
    notes: Optional[str] = None


class ConversationCreateRequest(BaseModel):
    """Empty body is fine — title is auto-derived from the first user
    message if not provided here. ``model`` pins a default for every
    turn in this conversation."""
    title: Optional[str] = None
    model: Optional[str] = None


class InviteAcceptRequest(BaseModel):
    """Optional info the redeemer may attach when accepting.

    The invite code itself is in the URL path, not this body.

    ``tos_accepted`` must be true for the redemption to succeed —
    the redemption page submits it as part of the form. The flag is
    here (not coerced server-side) so a programmatic redeemer cannot
    bypass the click-through accidentally.
    """
    invitee_email: Optional[str] = None
    tos_accepted: bool = False


class LoginRequest(BaseModel):
    """Username + password sign-in. The coordinator validates the pair
    against the argon2 hash on ``members.password_hash`` and, on
    success, mints a fresh bearer token (rotating the prior one). The
    caller stores the new token as their session credential."""
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    """Authenticated password change. Current password is required so a
    stolen session cookie cannot silently rotate the secret."""
    current_password: str
    new_password: str


class JobCancelRequest(BaseModel):
    """Member-initiated cancel. The caller's session identifies the
    submitter; the coordinator verifies ownership of the job before
    marking it cancelled. The worker (if still processing) discovers
    the cancellation when its /jobs/complete returns 410."""
    job_id: str


class JobRecord(BaseModel):
    job_id: str
    prompt: str
    status: str
    worker_id: Optional[str] = None
    model: Optional[str] = None
    text: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    earnings: Optional[float] = None
    duration_seconds: Optional[float] = None
    submitted_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    attempts: int = 0
