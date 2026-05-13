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
from fastapi.staticfiles import StaticFiles

from client.routes import admin_ui, api, auth, chat, invites

app = FastAPI(title="GamerAI Web UI")

# Vendored JS lives in client/static/ alongside this module. Serving
# our own copies of marked.js + DOMPurify removes the jsDelivr
# supply-chain dependency the chat UI used to have — a CDN compromise
# would otherwise let an attacker inject arbitrary JS into every
# authenticated page. Same logic as the Ollama-installer mirror.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(invites.router)
app.include_router(admin_ui.router)
app.include_router(api.router)
