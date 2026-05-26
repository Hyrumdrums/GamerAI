"""FastAPI app factory for the user-facing web UI.

Wires:

* ``/static`` — vendored JS at root (``marked.min.js``, ``purify.min.js``)
  plus per-page CSS and JS under ``css/``, ``js/``.
* :mod:`client.routes.auth` — ``/login``, ``/logout``.
* :mod:`client.routes.chat` — ``/``, ``/admin`` redirect, ``/dashboard``.
* :mod:`client.routes.invites` — public ``/invite/{code}`` flow.
* :mod:`client.routes.admin_ui` — ``/admin/members``, ``/admin/invites``.
* :mod:`client.routes.api` — every ``/api/*`` BFF proxy.

Where to add a new page:

1. HTML → ``client/templates/<name>.html.j2`` (extend ``base.html.j2``).
2. Styles → ``client/static/css/<name>.css``; link it from the template's
   ``extra_css`` block.
3. Scripts → ``client/static/js/<name>.js``; link it from the
   ``extra_js`` block.
4. Route handler → the matching ``client/routes/<feature>.py`` (create
   a new module + ``include_router`` here if it doesn't fit).
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from client.routes import account, admin_ui, api, auth, chat, invites

app = FastAPI(title="GamerAI Web UI")

# Vendored JS lives in client/static/ alongside this module. Serving
# our own copies of marked.js + DOMPurify removes the jsDelivr
# supply-chain dependency the chat UI used to have — a CDN compromise
# would otherwise let an attacker inject arbitrary JS into every
# authenticated page. Same logic as the Ollama-installer mirror.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    # The service worker source lives under /static for normal asset
    # organization, but a SW's scope is the path of the file it's
    # served from — /static/sw.js could only control /static/*. We
    # need root scope (/) so the SW intercepts navigations to /,
    # /login, /chat history, etc. Serving from /sw.js with the
    # Service-Worker-Allowed: / header gets us that.
    #
    # Template-substitute @@CACHE_VERSION@@ with the canonical value
    # from shared/config.py so a single bump in Python invalidates the
    # cache for every PWA install. Without this we'd have to bump
    # sw.js manually every release — the May 26 voice-mode ship was
    # served stale JS for exactly that reason.
    from shared.config import CLIENT_CACHE_VERSION
    src = (_STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    src = src.replace("@@CACHE_VERSION@@", f"v{CLIENT_CACHE_VERSION}")
    return Response(
        content=src,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            # No long-term caching of the SW itself — browsers
            # byte-compare the SW source to detect updates, so a
            # stale-while-revalidate CDN response could trap users
            # on an old version.
            "Cache-Control": "no-cache",
        },
    )


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(invites.router)
app.include_router(account.router)
app.include_router(admin_ui.router)
app.include_router(api.router)
