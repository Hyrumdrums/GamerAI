"""Shared pydantic schemas for the coordinator API."""
from typing import Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None


class GenerateResponse(BaseModel):
    job_id: str


class WorkerIdent(BaseModel):
    worker_id: str


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
