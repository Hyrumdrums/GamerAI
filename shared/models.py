"""Shared pydantic schemas for the coordinator API."""
from typing import List, Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str


class WorkerCapabilities(BaseModel):
    """What a worker advertises it can run.

    All fields are optional so a legacy ``/register`` call carrying just a
    ``worker_id`` keeps working — capabilities are additive.
    """
    vram_gb: Optional[float] = None
    gpu_model: Optional[str] = None       # e.g. "RTX 4090"
    bandwidth_class: Optional[str] = None  # "low" | "medium" | "high"
    models: List[str] = Field(default_factory=list)
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


class InviteCreateRequest(BaseModel):
    """Contributor (or admin) creates an invite for an outside person.

    All fields optional. ``daily_quota_tokens`` defaults to NULL =
    unlimited; ``expires_hours`` defaults to no expiry.
    """
    daily_quota_tokens: Optional[int] = None
    invitee_email: Optional[str] = None
    expires_hours: Optional[float] = None
    notes: Optional[str] = None


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
