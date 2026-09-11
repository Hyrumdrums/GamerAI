"""OpenAI-compatible chat surface, so a self-serve API key (see
coordinator/api_keys.py) works with the broad ecosystem of tools that
expect an OpenAI-shaped API — Home Assistant's Ollama/OpenAI integration,
Open WebUI's "connections", the official openai SDK, etc. — instead of
GamerAI's own native async ``/generate`` + poll-``/result`` contract.

    POST /v1/chat/completions   member (sync JSON or ``stream: true`` SSE)
    GET  /v1/models             member

This is purely a translation layer in front of the *existing* job queue —
it enqueues through the same ``/generate`` handler (reusing its quota,
idempotency, live-worker, and capacity checks verbatim) and reads results
through the same ``/result`` handler, just internally rather than over
HTTP. The worker/agent long-poll pipeline (``/jobs/next``, heartbeats,
``/jobs/complete``) is completely untouched — it doesn't know or care that
a request arrived via this endpoint instead of the native one.

``generate``/``result`` are injected as callables at registration time
(``main.py``'s own, already-tested functions) rather than imported, since
they're deeply coupled to a large cluster of other main.py-local helpers
(quota checks, conversation handling, image/search rewrite dispatch) that
aren't worth relocating just for this — same closure-over-dependencies
shape coordinator/notifications.py and coordinator/uploads.py already use
for ``db``, extended here to two more callables. main.py wires it once:
``app.include_router(openai_compat.build_router(db, generate, result,
model_registry))``.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from shared.config import V1_CHAT_TIMEOUT_SECONDS
from shared.models import GenerateRequest, OpenAIChatCompletionRequest

_POLL_INTERVAL_SECONDS = 0.2  # matches client/static/js/streamingEngine.js's own poll cadence


def _openai_error_type(status_code: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "authentication_error",
        404: "invalid_request_error",
        429: "rate_limit_error",
        503: "upstream_error",
        504: "timeout",
    }.get(status_code, "api_error")


def _openai_error_body(status_code: int, message: str, error_type: Optional[str] = None) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type or _openai_error_type(status_code),
            "param": None,
            "code": status_code,
        }
    }


def _sse_chunk(completion_id: str, created: int, model: Optional[str], delta: dict, finish_reason: Optional[str] = None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def build_router(db, generate_fn, result_fn, model_registry) -> APIRouter:
    router = APIRouter()

    def _poll_chat_completion_blocking(job_id: str, model: Optional[str], request: Request):
        deadline = time.time() + V1_CHAT_TIMEOUT_SECONDS
        data = result_fn(job_id, request)
        while not data.get("done") and time.time() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            data = result_fn(job_id, request)
        if not data.get("done"):
            return JSONResponse(
                _openai_error_body(504, "generation timed out"), status_code=504,
            )
        if data.get("status") == "error":
            return JSONResponse(
                _openai_error_body(502, data.get("error") or "generation failed", "upstream_error"),
                status_code=502,
            )
        prompt_tokens = data.get("prompt_tokens") or 0
        completion_tokens = data.get("completion_tokens") or 0
        return {
            "id": f"chatcmpl-{job_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or data.get("model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": data.get("text") or ""},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _stream_chat_completion(job_id: str, model: Optional[str], request: Request):
        completion_id = f"chatcmpl-{job_id}"
        created = int(time.time())
        sent = ""
        deadline = time.time() + V1_CHAT_TIMEOUT_SECONDS
        yield _sse_chunk(completion_id, created, model, {"role": "assistant"})
        while True:
            data = result_fn(job_id, request)
            text = data.get("text") or ""
            if len(text) > len(sent):
                yield _sse_chunk(completion_id, created, model, {"content": text[len(sent):]})
                sent = text
            if data.get("done") or time.time() > deadline:
                yield _sse_chunk(completion_id, created, model, {}, finish_reason="stop")
                yield "data: [DONE]\n\n"
                return
            time.sleep(_POLL_INTERVAL_SECONDS)

    @router.post("/v1/chat/completions")
    def openai_chat_completions(req: OpenAIChatCompletionRequest, request: Request):
        last_user = next(
            (m.content for m in reversed(req.messages) if m.role == "user"), None,
        )
        if not last_user:
            return JSONResponse(
                _openai_error_body(400, "at least one user message required"),
                status_code=400,
            )
        gen_req = GenerateRequest(
            prompt=last_user,
            model=req.model,
            tool="chat",
            messages=[m.model_dump() for m in req.messages],
        )
        try:
            gen_resp = generate_fn(gen_req, request)
        except HTTPException as exc:
            return JSONResponse(
                _openai_error_body(exc.status_code, str(exc.detail)),
                status_code=exc.status_code,
            )
        if req.stream:
            return StreamingResponse(
                _stream_chat_completion(gen_resp.job_id, req.model, request),
                media_type="text/event-stream",
            )
        return _poll_chat_completion_blocking(gen_resp.job_id, req.model, request)

    @router.get("/v1/models")
    def openai_list_models():
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {"id": m.name, "object": "model", "created": now, "owned_by": "gamerai"}
                for m in model_registry.list_all()
                if m.kind == "chat"
            ],
        }

    return router
