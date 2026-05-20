"""Shared pydantic schemas for the coordinator API."""
from typing import List, Optional

from pydantic import BaseModel, Field


class ImageParams(BaseModel):
    """Optional per-image-job knobs. Defaults are set to match the
    default model's recommendation (see model_registry.IMAGE_CATALOG)
    so a UI that sends `{tool: "image"}` with no params still produces
    a sensible image."""
    width: int = 512
    height: int = 512
    steps: int = 20
    seed: Optional[int] = None
    negative_prompt: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    # "chat" (default, legacy) or "image". Image jobs route to a
    # different Redis queue (job_queue:image) and only image-capable
    # workers pick them up. Defaulting to "chat" keeps every existing
    # client (CLI, agent canaries, retry path) working unchanged.
    tool: str = "chat"
    # Image-only knobs. Ignored for tool="chat". When tool="image" and
    # omitted, the model's defaults are used.
    image: Optional[ImageParams] = None
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
    correctly overwrites any leftover text from the original attempt."""
    worker_id: str
    job_id: str
    text: str


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


class JobClaimRequest(BaseModel):
    worker_id: str
    job_id: str


class JobNextRequest(BaseModel):
    """Worker pulls the next job from a queue. ``tool`` selects which
    per-tool Redis queue to LPOP. Defaults to "chat" so legacy agents
    (no tool field) keep pulling chat jobs. Image-capable agents call
    with tool="image" to pull from job_queue:image."""
    worker_id: str
    tool: str = "chat"


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
