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
    # "chat" (default, legacy), "image", "search", or "tts". Each tool
    # routes to its own Redis queue and is only picked up by workers
    # that advertise the matching capability — chat-only agents never
    # get handed an image / search / tts job. TTS rides on the
    # contributor's CPU loop (Piper, see voice-phase1), so a worker
    # mid-chat can serve TTS concurrently from its idle CPU.
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
    # Chat-only flag. When true, the agent's chat handler pipelines
    # Piper TTS with LLM inference: as soon as the first sentence is
    # emitted, it spawns a CPU-side synth in parallel and ships the
    # audio back on a partial result alongside the streaming text;
    # the rest of the response is synthesized once the LLM finishes
    # and attached to the final result. The client buffers text
    # rendering until first audio is in hand so voice + text reveal
    # together. Ignored for tool != "chat". Default false keeps the
    # text-only path bit-identical to before.
    voice_mode: bool = False


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
    # Voice-mode chat exponential batching: each completed Piper synth
    # for a sentence-batch posts a partial carrying one chunk. seq is
    # monotonic from 0; batches are sized 1, 2, 4, 8, ... so first
    # audio plays fast and later synths have more playback time to
    # absorb their longer wall-clock. Coordinator stores each chunk
    # in JOB_AUDIO_CHUNKS keyed by seq so it can return the full list
    # on /result and tolerate duplicates from a retried partial.
    audio_chunk_b64: Optional[str] = None
    audio_chunk_seconds: Optional[float] = None
    audio_chunk_seq: Optional[int] = None


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
    # For tts jobs: the synthesized audio (Opus/ogg) as base64. Unlike
    # images, TTS audio is ephemeral — the coordinator stuffs it into
    # JOB_RESULTS for the client to play and never persists to disk.
    # Empty/absent on chat / image / search jobs.
    audio_b64: Optional[str] = None
    # For tts jobs: the playback duration of the synthesized audio,
    # measured by the worker. This is what bills against the user's
    # voice_minutes daily cap (NOT duration_seconds, which is wall-
    # clock synthesis time and unrelated to perceived "minutes of
    # voice consumed"). Float so sub-second sentences carry their
    # real cost — client-side segmentation produces a lot of short
    # clauses that would round to zero in integer minutes.
    audio_seconds: Optional[float] = None
    # For voice-mode chat jobs: full ordered list of audio chunks
    # emitted during the LLM stream, re-sent here so a client that
    # reloaded the page after completion still has the whole audio.
    # Each entry: {"seq": int, "audio_b64": str, "audio_seconds": float}.
    # Empty/absent on non-voice chat / image / search / tool=tts jobs.
    # voice_minutes billing sums audio_seconds across all entries.
    audio_chunks: Optional[List[dict]] = None
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

    ``invitee_email`` is required — every member needs an email on
    file so the deferred email-based password reset has a target. The
    redemption form pre-fills this value and can override it (the
    friend types their own address), but having one upfront helps the
    host identify which open invite is which and gives us a fallback
    address if the redemption flow somehow ships without one.

    ``daily_quota_tokens`` and ``daily_quota_images`` are two-dimensional
    quotas; either can be NULL = unlimited. The pair is inherited onto
    the new member row at redemption and enforced independently in
    /generate's quota gate.
    ``expires_hours`` defaults to no expiry.
    """
    invitee_email: str
    daily_quota_tokens: Optional[int] = None
    daily_quota_images: Optional[int] = None
    expires_hours: Optional[float] = None
    notes: Optional[str] = None


class ConversationCreateRequest(BaseModel):
    """Empty body is fine — title is auto-derived from the first user
    message if not provided here. ``model`` pins a default for every
    turn in this conversation."""
    title: Optional[str] = None
    model: Optional[str] = None


class InviteAcceptRequest(BaseModel):
    """The redeemer's chosen credentials. The invite code itself is the
    one-shot redemption credential and lives in the URL path; this body
    is what the new member's account will be configured with.

    ``username`` and ``password`` create a u/p login the friend can use
    going forward — no bearer token paste, no email service in the loop.
    ``invitee_email`` is required for recognizability (the host needs to
    know which friend just accepted) and is also where any future
    email-based password reset will go.

    ``tos_accepted`` must be true for the redemption to succeed — the
    redemption page submits it as part of the form. The flag is here
    (not coerced server-side) so a programmatic redeemer cannot bypass
    the click-through accidentally.
    """
    username: str
    password: str
    invitee_email: str
    tos_accepted: bool = False


class FriendQuotaUpdateRequest(BaseModel):
    """Host edits an accepted invitee's two-dimensional daily cap.
    Both fields are sent on every update — the form always carries
    the new full state. ``None`` on either axis means "unlimited";
    a numeric value pins that dimension. There's no partial-update
    semantic to keep the wire contract obvious."""
    daily_quota_tokens: Optional[int] = None
    daily_quota_images: Optional[int] = None


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


class AgentPairPollRequest(BaseModel):
    """Agent's polling body during browser-handoff pairing.

    Carries only the short pairing code; the request is unauthenticated
    because the agent does not yet have a token (that's the whole point
    of pairing). The code itself is the credential — leaking it lets a
    bystander steal the token once the user clicks Confirm in the
    browser, so we keep the TTL short (default 5 minutes).
    """
    pair_code: str


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
