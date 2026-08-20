"""No-install public demo chat. Unlike every other page in this
module, this one requires no session — the point is that someone
evaluating the project (an interviewer, a curious visitor) can try it
without an account, an invite, or a locally-running agent.

Entirely client-side: the page ships a small library of canned
responses and picks one locally on submit (see demo.html.j2's inline
script). No prompt ever reaches the coordinator or a real worker —
there is nothing here for a bot to spam into real inference cost,
because there's no backend call to spam."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from client.templating import templates

router = APIRouter()


@router.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    return templates.TemplateResponse(request, "demo.html.j2", {})
