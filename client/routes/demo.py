"""No-install public demo chat. Unlike every other page in this
module, this one requires no session — the point is that someone
evaluating the project (an interviewer, a curious visitor) can try it
without an account, an invite, or a locally-running agent.

The page itself (``GET /demo``) is plain server-rendered HTML with a
small inline script — deliberately not wired into the real chat UI's
``chat.js`` (conversations, streaming, markdown rendering, voice mode),
which all assume an authenticated session. The two ``/api/demo/*``
proxies below forward to the coordinator's public ``POST /demo/generate``
/ ``GET /demo/result/{job_id}`` with no bearer, same shape as every
other ``/api/*`` BFF proxy in :mod:`client.routes.api`."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from client.services import api_client
from client.templating import templates

router = APIRouter()


@router.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    return templates.TemplateResponse(request, "demo.html.j2", {})


@router.post("/api/demo/generate")
async def proxy_demo_generate(payload: dict, request: Request):
    return await api_client.proxy_json(
        bearer=None,
        method="POST", path="/demo/generate",
        json=payload, text_fallback=True,
    )


@router.get("/api/demo/result/{job_id}")
async def proxy_demo_result(job_id: str, request: Request):
    return await api_client.proxy_json(
        bearer=None,
        method="GET", path=f"/demo/result/{job_id}",
    )
