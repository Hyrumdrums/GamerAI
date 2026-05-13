"""Tiny FastAPI web UI for GamerAI. Submit prompts, browse workers/earnings/metrics."""
import html as html_lib
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared.auth import auth_headers

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")

# Session cookie name + lifetime. The cookie value is literally the
# user's bearer token. HttpOnly prevents JavaScript from reading it,
# Secure keeps it off plaintext HTTP. SameSite=Lax stops a third-party
# site from triggering authenticated requests on our user's behalf.
SESSION_COOKIE = "gai_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
# In dev (no HTTPS) the Secure flag would prevent the cookie from
# being set at all. The PUBLIC_BASE_URL env is set to the public
# https://... URL on the VPS; we use that as the signal.
COOKIE_SECURE = (os.getenv("PUBLIC_BASE_URL", "http://").startswith("https://"))

app = FastAPI(title="GamerAI Web UI")


def _client(bearer: Optional[str] = None) -> httpx.AsyncClient:
    """httpx client for the coordinator.

    When ``bearer`` is provided, sends ``Authorization: Bearer <bearer>``
    (the per-session user's token). When omitted, falls back to the
    admin token from the env (legacy path used by admin browser
    views). Both forms talk to the same coordinator API — the only
    difference is which member they authenticate as.
    """
    if bearer:
        return httpx.AsyncClient(
            base_url=COORDINATOR_URL,
            headers={"Authorization": f"Bearer {bearer}"},
        )
    return httpx.AsyncClient(base_url=COORDINATOR_URL, headers=auth_headers())


def _public_client() -> httpx.AsyncClient:
    """httpx client for the public invite-redemption flow. Sends no
    Authorization header — the invite code itself is the credential."""
    return httpx.AsyncClient(base_url=COORDINATOR_URL)


# ---------- session helpers ----------
def _session_bearer(request: Request) -> Optional[str]:
    """Extract the user's bearer token from the session cookie.
    None when not logged in."""
    return request.cookies.get(SESSION_COOKIE) or None


async def _identify(bearer: str) -> Optional[dict]:
    """Resolve a bearer to the /me payload, or None if invalid.
    Caches nothing — every request re-checks so revoked tokens stop
    working immediately."""
    if not bearer:
        return None
    try:
        async with _client(bearer=bearer) as c:
            r = await c.get("/me", timeout=5)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("auth_disabled"):
            # Auth is off coordinator-side; treat anyone as a logged-in
            # admin so dev/test loops don't have to plumb env.
            return {"member_id": "dev", "role": "admin", "email": None}
        return body
    except httpx.HTTPError:
        return None


def _set_session_cookie(response, bearer: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, bearer,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _login_redirect(next_path: str = "/") -> RedirectResponse:
    target = f"/login?next={next_path}" if next_path != "/" else "/login"
    return RedirectResponse(target, status_code=303)

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>GamerAI</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{font-family:-apple-system,system-ui,sans-serif;color:#1a1a1a;background:#f7f7f8;display:flex;flex-direction:column}
  .topbar{display:flex;justify-content:space-between;align-items:center;padding:.5rem 1rem;background:#fff;border-bottom:1px solid #e5e5e5;font-size:.9rem}
  .topbar a{color:#2d6cdf;text-decoration:none;margin-left:1rem}
  .topbar .brand{font-weight:600;color:#1a1a1a}
  .layout{display:flex;flex:1;min-height:0}
  .sidebar{width:260px;background:#fff;border-right:1px solid #e5e5e5;display:flex;flex-direction:column}
  .sidebar-head{padding:.75rem 1rem;border-bottom:1px solid #f0f0f0;display:flex;gap:.5rem}
  .sidebar-head button{flex:1;padding:.5rem;font-size:.9rem;background:#2d6cdf;color:#fff;border:0;border-radius:4px;cursor:pointer}
  .sidebar-head button:hover{background:#1f55b8}
  .conv-list{flex:1;overflow-y:auto;padding:.25rem 0}
  .conv-item{padding:.6rem 1rem;cursor:pointer;border-left:3px solid transparent;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#333}
  .conv-item:hover{background:#f5f5f5}
  .conv-item.active{background:#eef3fb;border-left-color:#2d6cdf;color:#1a1a1a;font-weight:500}
  .main{flex:1;display:flex;flex-direction:column;min-width:0;background:#fff}
  .chat-pane{flex:1;overflow-y:auto;padding:1.5rem;display:flex;flex-direction:column;gap:1rem}
  .msg{max-width:46rem;width:100%;align-self:center;display:flex;gap:.75rem}
  .msg .bubble{flex:1;padding:.6rem .9rem;border-radius:6px;line-height:1.55;font-size:.95rem;word-wrap:break-word}
  .msg.user .bubble{background:#eef3fb;color:#1a1a1a}
  .msg.assistant .bubble{background:#fafafa;color:#1a1a1a;border:1px solid #efefef}
  .msg .role{font-size:.75rem;color:#666;flex:0 0 4rem;text-transform:uppercase;padding-top:.4rem}
  .msg .bubble pre{background:#f3f3f3;padding:.5rem;border-radius:4px;overflow-x:auto;font-size:.85em;margin:.4rem 0}
  .msg .bubble code{background:#f3f3f3;padding:.05rem .25rem;border-radius:3px;font-size:.9em}
  .msg .bubble pre code{background:transparent;padding:0}
  .msg .bubble p:first-child{margin-top:0}
  .msg .bubble p:last-child{margin-bottom:0}
  .empty{align-self:center;color:#888;margin-top:4rem;font-size:.9rem;text-align:center}
  .empty h2{color:#444;font-weight:500;margin-bottom:.5rem}
  .composer{border-top:1px solid #e5e5e5;padding:1rem 1.5rem;background:#fff}
  .composer-inner{max-width:48rem;margin:0 auto;display:flex;gap:.5rem;align-items:flex-end}
  textarea{flex:1;min-height:2.5rem;max-height:12rem;font-size:1rem;padding:.6rem;border:1px solid #ccc;border-radius:6px;resize:none;font-family:inherit;line-height:1.4}
  textarea:focus{outline:0;border-color:#2d6cdf}
  .composer button{padding:.55rem 1rem;font-size:1rem;background:#2d6cdf;color:#fff;border:0;border-radius:4px;cursor:pointer}
  .composer button:disabled{background:#aaa;cursor:not-allowed}
  .status{max-width:48rem;margin:.5rem auto 0;color:#666;font-size:.8rem;text-align:center;min-height:1em}
  .typing{display:inline-block;color:#666;font-style:italic}
</style></head>
<body>
<div class="topbar">
  <div class="brand">GamerAI</div>
  <div>
    <span id="who">signing in…</span>
    <a href="/tos" target="_blank">terms</a>
    <a id="adminlink" href="/dashboard" hidden>admin</a>
    <a href="/logout">sign out</a>
  </div>
</div>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-head">
      <button id="new-chat">+ New chat</button>
    </div>
    <div class="conv-list" id="conv-list"></div>
  </aside>

  <main class="main">
    <div class="chat-pane" id="chat-pane">
      <div class="empty" id="empty-state">
        <h2>What's on your mind?</h2>
        <div>Start a new conversation by typing below.</div>
      </div>
    </div>

    <form class="composer" id="composer">
      <div class="composer-inner">
        <textarea id="prompt" placeholder="Message GamerAI..." rows="1"></textarea>
        <button type="submit" id="submit">Send</button>
      </div>
      <div class="status" id="status"></div>
    </form>
  </main>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js" integrity="sha384-r93nUxgK4UnyZc3qmoCKKqZpsR1tNvHohKVPK16BzMc1+kgwY3RyP8+ZIyx8RGy/" crossorigin="anonymous"></script>
<script>
// ---- state ------------------------------------------------------------
let me = null;
let conversations = [];   // [{conversation_id, title, updated_at, ...}]
let currentId = null;     // active conversation_id, or null = brand-new

// ---- bootstrap --------------------------------------------------------
async function init() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { location.href = '/login'; return; }
    me = await r.json();
    document.getElementById('who').textContent =
      `${me.email || me.member_id || 'signed in'} · ${me.role || 'member'}`;
    if (me.role === 'admin') {
      document.getElementById('adminlink').hidden = false;
    }
  } catch { location.href = '/login'; return; }
  await refreshSidebar();
  if (conversations.length > 0) {
    openConversation(conversations[0].conversation_id);
  }
}
init();

// ---- sidebar ----------------------------------------------------------
async function refreshSidebar() {
  const r = await fetch('/api/conversations');
  if (!r.ok) return;
  const body = await r.json();
  conversations = body.conversations || [];
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  for (const c of conversations) {
    const div = document.createElement('div');
    div.className = 'conv-item' + (c.conversation_id === currentId ? ' active' : '');
    div.textContent = c.title || '(untitled)';
    div.title = c.title || c.conversation_id;
    div.onclick = () => openConversation(c.conversation_id);
    list.appendChild(div);
  }
}

document.getElementById('new-chat').onclick = () => {
  currentId = null;
  document.getElementById('chat-pane').innerHTML =
    '<div class="empty"><h2>What\\'s on your mind?</h2><div>Start a new conversation by typing below.</div></div>';
  document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
  document.getElementById('prompt').focus();
};

// ---- conversation rendering ------------------------------------------
async function openConversation(id) {
  currentId = id;
  // Highlight the right sidebar entry.
  document.querySelectorAll('.conv-item').forEach((el, i) => {
    el.classList.toggle('active', conversations[i] && conversations[i].conversation_id === id);
  });
  const r = await fetch('/api/conversations/' + id);
  if (!r.ok) return;
  const body = await r.json();
  renderMessages(body.messages || []);
}

function renderMessages(messages) {
  const pane = document.getElementById('chat-pane');
  pane.innerHTML = '';
  if (messages.length === 0) {
    pane.innerHTML = '<div class="empty"><h2>(empty)</h2></div>';
    return;
  }
  for (const m of messages) {
    pane.appendChild(messageEl(m.role, m.text));
  }
  pane.scrollTop = pane.scrollHeight;
}

function messageEl(role, text) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const r = document.createElement('div');
  r.className = 'role';
  r.textContent = role;
  const b = document.createElement('div');
  b.className = 'bubble';
  // Render assistant messages as markdown; user messages stay literal
  // to avoid surprising rendering of pasted code.
  if (role === 'assistant' && window.marked) {
    b.innerHTML = window.marked.parse(text || '');
  } else {
    b.textContent = text;
  }
  wrap.appendChild(r);
  wrap.appendChild(b);
  return wrap;
}

// ---- composer ---------------------------------------------------------
const textarea = document.getElementById('prompt');
textarea.addEventListener('input', () => {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 12 * 16) + 'px';
});
textarea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    document.getElementById('composer').requestSubmit();
  }
});

document.getElementById('composer').onsubmit = async (e) => {
  e.preventDefault();
  const prompt = textarea.value.trim();
  if (!prompt) return;
  const submitBtn = document.getElementById('submit');
  const statusEl = document.getElementById('status');
  submitBtn.disabled = true;
  textarea.disabled = true;

  // If this is a brand-new chat (no currentId), create one first.
  if (!currentId) {
    const cr = await fetch('/api/conversations', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (!cr.ok) {
      statusEl.textContent = 'failed to create conversation';
      submitBtn.disabled = false; textarea.disabled = false;
      return;
    }
    currentId = (await cr.json()).conversation_id;
  }

  // Append the user message optimistically so it shows up right away.
  const pane = document.getElementById('chat-pane');
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();
  pane.appendChild(messageEl('user', prompt));
  const typing = messageEl('assistant', '');
  typing.querySelector('.bubble').innerHTML = '<span class="typing">thinking…</span>';
  pane.appendChild(typing);
  pane.scrollTop = pane.scrollHeight;
  textarea.value = '';
  textarea.style.height = 'auto';

  // Submit + poll.
  statusEl.textContent = 'submitting…';
  const start = Date.now();
  const gr = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, conversation_id: currentId}),
  });
  if (gr.status === 401) { location.href = '/login'; return; }
  if (!gr.ok) {
    typing.remove();
    statusEl.textContent = 'error: ' + gr.status + ' ' + (await gr.text());
    submitBtn.disabled = false; textarea.disabled = false;
    return;
  }
  const {job_id} = await gr.json();
  while (true) {
    await new Promise(r => setTimeout(r, 1000));
    const res = await fetch('/api/result/' + job_id).then(r => r.json());
    if (res.status === 'complete' || res.status === 'error') {
      typing.remove();
      const txt = res.text || res.error || '(empty)';
      pane.appendChild(messageEl('assistant', txt));
      pane.scrollTop = pane.scrollHeight;
      const dt = ((Date.now() - start) / 1000).toFixed(1);
      statusEl.textContent =
        `done in ${dt}s · ${res.completion_tokens || 0} tokens · ${res.worker_id || 'unknown worker'}`;
      submitBtn.disabled = false; textarea.disabled = false;
      textarea.focus();
      // Refresh the sidebar so the title (set from the first prompt
      // on the coordinator) shows up.
      await refreshSidebar();
      // Re-highlight current.
      document.querySelectorAll('.conv-item').forEach((el, i) => {
        el.classList.toggle('active', conversations[i] && conversations[i].conversation_id === currentId);
      });
      return;
    }
  }
};
</script>
</body></html>
"""


_LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in — GamerAI</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:480px;margin:4rem auto;padding:0 1rem;color:#1a1a1a}}
  h1{{margin-bottom:.25rem}}
  .sub{{color:#666;margin-bottom:1.5rem}}
  input[type=password]{{width:100%;padding:.6rem;font-size:1rem;font-family:ui-monospace,Menlo,Consolas,monospace;box-sizing:border-box;margin-bottom:.75rem}}
  button{{font-size:1rem;padding:.6rem 1.2rem;cursor:pointer;background:#2d6cdf;color:#fff;border:0;border-radius:4px;width:100%}}
  button:hover{{background:#1f55b8}}
  .err{{background:#fde0e0;border:1px solid #f5b0b0;color:#900;padding:.6rem .9rem;border-radius:6px;margin-bottom:1rem}}
  .hint{{color:#666;font-size:.85rem;margin-top:1rem}}
  a{{color:#2d6cdf}}
</style></head>
<body>
<h1>GamerAI</h1>
<div class="sub">Sign in with your bearer token to start a session.</div>

{error_block}

<form method="POST" action="/login">
  <input type="hidden" name="next" value="{next_path}">
  <input type="password" name="token" placeholder="gai_<your token>" autocomplete="off" autofocus required>
  <button type="submit">Sign in</button>
</form>

<div class="hint">
  No token? Ask the contributor who invited you for a fresh invite link,
  or read the <a href="/tos">community terms</a>.
</div>
</body></html>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    # If already logged in, bounce to the destination.
    bearer = _session_bearer(request)
    if bearer and await _identify(bearer):
        return RedirectResponse(next or "/", status_code=303)
    return HTMLResponse(_LOGIN_PAGE.format(
        next_path=html_lib.escape(next or "/"),
        error_block="",
    ))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    token: str = Form(...),
    next: str = Form("/"),
):
    bearer = token.strip()
    me = await _identify(bearer)
    if me is None:
        # Invalid or revoked token — re-show the form with an error.
        return HTMLResponse(_LOGIN_PAGE.format(
            next_path=html_lib.escape(next or "/"),
            error_block='<div class="err">That token was rejected by the coordinator. Double-check it and try again.</div>',
        ), status_code=401)
    safe_next = next if next.startswith("/") else "/"
    response = RedirectResponse(safe_next, status_code=303)
    _set_session_cookie(response, bearer)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    _clear_session_cookie(response)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    bearer = _session_bearer(request)
    if not bearer or not await _identify(bearer):
        return _login_redirect("/")
    return HTMLResponse(INDEX_HTML)


@app.get("/admin")
def admin_redirect():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    bearer = _session_bearer(request)
    me = await _identify(bearer) if bearer else None
    if me is None:
        return _login_redirect("/dashboard")
    if me.get("role") != "admin":
        return HTMLResponse(
            "<h1>403 — admin only</h1><p>Dashboard requires the admin role.</p>"
            '<p><a href="/">Back to chat</a></p>',
            status_code=403,
        )
    async with _client(bearer=bearer) as c:
        m = (await c.get("/metrics", timeout=5)).json()
        w = (await c.get("/workers", timeout=5)).json()
        e = (await c.get("/earnings", timeout=5)).json()

    workers = w["workers"]
    active_workers = [w for w in workers if w["status"] == "online"]
    total_earnings = sum(w.get('total_usd', 0) for w in workers)

    worker_rows = "".join(
        f"<tr><td><span style='color:{'#4CAF50' if x['status'] == 'online' else '#FF5722'};'>●</span> "
        f"{x['worker_id'][-12:]}</td><td>{x['status']}</td>"
        f"<td>{x['total_jobs']}</td><td>{x['total_tokens']}</td>"
        f"<td>${x['total_usd']:.6f}</td></tr>"
        for x in workers
    )
    
    # Metrics table
    metrics_html = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in m.items())
    
    # Recent earnings
    recent_earnings = ""
    if e.get("workers"):
        recent_earnings = "".join(
            f"<tr><td>{w['worker_id'][-12:]}</td><td>{w['total_jobs']}</td>"
            f"<td>{w['total_tokens']}</td><td>${w['total_usd']:.6f}</td></tr>"
            for w in sorted(e["workers"], key=lambda x: x["total_usd"], reverse=True)[:5]
        )
    
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>🎮 GamerAI Admin Dashboard</title>
<style>
body{{font-family:system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;background:#f8f9fa}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem;padding:1rem;background:white;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem}}
.stat-card{{padding:1.5rem;background:white;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);text-align:center}}
.stat-value{{font-size:2rem;font-weight:bold;color:#2d6cdf}}
.stat-label{{color:#666;font-size:0.9rem;margin-top:0.5rem}}
.section{{background:white;margin-bottom:2rem;padding:1.5rem;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}
h1{{margin:0;color:#333}}h2{{margin:0 0 1rem 0;color:#555;font-size:1.3rem}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{padding:.6rem;border-bottom:1px solid #eee;text-align:left}}
th{{background:#f8f9fa;font-weight:600}}
a{{color:#2d6cdf;text-decoration:none}}
.refresh-btn{{padding:0.5rem 1rem;background:#2d6cdf;color:white;border:none;border-radius:4px;cursor:pointer}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🎮 GamerAI Admin Dashboard</h1>
    <div style="color:#666">Distributed AI Inference Marketplace</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
    <a href="/" style="margin-left:1rem">← Back to Client</a>
  </div>
</div>

<div class="stats">
  <div class="stat-card">
    <div class="stat-value">{len(workers)}</div>
    <div class="stat-label">Total Workers</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{len(active_workers)}</div>
    <div class="stat-label">Online Workers</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{m.get('total_jobs', 0)}</div>
    <div class="stat-label">Jobs Processed</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">${total_earnings:.4f}</div>
    <div class="stat-label">Total Earnings</div>
  </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:2rem">
  <div class="section">
    <h2>⚡ System Metrics</h2>
    <table><tr><th>Metric</th><th>Value</th></tr>{metrics_html}</table>
  </div>
  
  <div class="section">
    <h2>🏆 Top Earners</h2>
    <table><tr><th>Worker</th><th>Jobs</th><th>Tokens</th><th>Earnings</th></tr>{recent_earnings}</table>
  </div>
</div>

<div class="section">
  <h2>👥 All Workers</h2>
  <table>
    <tr><th>Worker ID</th><th>Status</th><th>Jobs</th><th>Tokens</th><th>Earnings</th></tr>
    {worker_rows}
  </table>
</div>

</body></html>"""
    return html


# ---------- invite redemption (public, no auth) ----------
_REDEEM_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>You've been invited — GamerAI</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:580px;margin:3rem auto;padding:0 1rem;color:#1a1a1a;line-height:1.5}}
  h1{{margin-bottom:.25rem}}
  .sub{{color:#666;margin-bottom:1.5rem}}
  .card{{background:#fafafa;border:1px solid #e5e5e5;border-radius:8px;padding:1.25rem;margin-bottom:1.25rem}}
  .label{{color:#666;font-size:.85rem}}
  .value{{font-weight:600;margin-bottom:.5rem}}
  input[type=email]{{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box;margin-bottom:.5rem}}
  button{{font-size:1rem;padding:.6rem 1.2rem;cursor:pointer;background:#2d6cdf;color:#fff;border:0;border-radius:4px}}
  button:hover{{background:#1f55b8}}
  button[disabled]{{background:#999;cursor:not-allowed}}
  .err{{color:#b00020}}
  .tos{{background:#fff8e8;border:1px solid #f0d57b;border-radius:8px;padding:1rem 1.1rem;margin-bottom:1rem;font-size:.92rem}}
  .tos h2{{margin:0 0 .5rem;font-size:1.05rem}}
  .tos ul{{margin:.25rem 0 .5rem 0;padding-left:1.25rem}}
  .tos li{{margin-bottom:.25rem}}
  .tos a{{color:#2d6cdf}}
  .accept-row{{display:flex;align-items:flex-start;gap:.5rem;margin:.75rem 0 1rem}}
  .accept-row input{{margin-top:.25rem}}
</style></head>
<body>
<h1>You've been invited</h1>
<div class="sub">Someone with a GamerAI contributor account wants to share their network with you.</div>

<div class="card">
  <div class="label">Invited by</div>
  <div class="value">{contributor}</div>
  <div class="label">Daily prompt cap</div>
  <div class="value">{cap}</div>
  {expiry_block}
</div>

<div class="tos">
  <h2>Community terms (TL;DR)</h2>
  <ul>
    <li><strong>Prompts are visible</strong> to whichever contributor's GPU serves them. Don't paste passwords, API keys, or anything sensitive.</li>
    <li><strong>Best-effort service.</strong> Things can break, timeouts happen, and answers can be wrong. Don't use this for anything that costs money or harms people if it fails.</li>
    <li><strong>Be a good neighbor.</strong> No abuse, no scraping, no illegal content. We'll remove members who break trust.</li>
  </ul>
  <a href="/tos" target="_blank">Read the full terms</a>
</div>

<form method="POST">
  <label for="email" class="label">Your email (optional, helps the inviter recognize you)</label>
  <input id="email" name="invitee_email" type="email" placeholder="you@example.com" />

  <div class="accept-row">
    <input id="tos_accepted" name="tos_accepted" type="checkbox" required />
    <label for="tos_accepted">I've read and accept the <a href="/tos" target="_blank">community terms</a>.</label>
  </div>

  <button id="submit" type="submit" disabled>Accept and get my token</button>
</form>

<script>
  const cb = document.getElementById('tos_accepted');
  const btn = document.getElementById('submit');
  cb.addEventListener('change', () => {{ btn.disabled = !cb.checked; }});
</script>
</body></html>
"""

_REDEEM_DONE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Welcome to GamerAI</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:2.5rem auto;padding:0 1rem;color:#1a1a1a}}
  h1{{margin-bottom:.25rem}}
  h2{{margin-top:2rem;font-size:1.15rem}}
  .sub{{color:#666;margin-bottom:1.5rem}}
  .token-row{{display:flex;gap:.5rem;align-items:stretch;margin-bottom:.25rem}}
  .token{{flex:1;font-family:ui-monospace,Menlo,Consolas,monospace;background:#fff8c4;padding:.75rem;border-radius:4px;font-size:1rem;word-break:break-all;border:1px solid #ddc97a;user-select:all}}
  .copy-btn{{font-size:.9rem;padding:.4rem .8rem;cursor:pointer;background:#444;color:#fff;border:0;border-radius:4px}}
  .copy-btn:hover{{background:#222}}
  .copy-btn.ok{{background:#1f8a3a}}
  .download-btn{{display:inline-block;font-size:1rem;padding:.7rem 1.4rem;cursor:pointer;background:#2d6cdf;color:#fff;border:0;border-radius:4px;text-decoration:none;font-weight:600}}
  .download-btn:hover{{background:#1f55b8}}
  .warn{{color:#b06000;font-weight:600;margin:1.25rem 0 .25rem}}
  .small{{color:#666;font-size:.9rem;margin-top:.5rem}}
  ol{{padding-left:1.25rem;line-height:1.55}}
  ol li{{margin-bottom:.5rem}}
  code{{background:#f3f3f3;padding:.1rem .35rem;border-radius:3px;font-size:.92em}}
  .note{{background:#f5f8ff;border:1px solid #cfd9ee;border-radius:6px;padding:.7rem .9rem;font-size:.9rem;color:#33425a;margin:.75rem 0}}
</style></head>
<body>
<h1>Welcome to GamerAI</h1>
<div class="sub">Your invitee account is live. Two quick steps and you're up.</div>

<div class="warn">1. Save your bearer token (it cannot be recovered)</div>
<div class="token-row">
  <div class="token" id="tok">{token}</div>
  <button class="copy-btn" id="copy" type="button">Copy</button>
</div>
<div class="small">
  member_id: <code>{member_id}</code> &nbsp;·&nbsp; daily_quota_tokens: <code>{cap}</code>
</div>

<h2>2. Install the Windows agent</h2>
<p>The agent runs in the background on a Windows PC and only takes work when the machine is idle. Power scales with demand, not uptime.</p>

<p><a class="download-btn" href="/download/GamerAI-Agent-Setup.exe">Download installer (Windows, ~12 MB)</a></p>

<div class="note">
  <strong>Windows SmartScreen note:</strong> the installer isn't code-signed yet,
  so Windows will pop a "Windows protected your PC" dialog the first time you
  run it. Click <em>More info</em> → <em>Run anyway</em>. The source is at
  github.com/Hyrumdrums/GamerAI; the binary is built by GitHub Actions on
  every push (see <code>/download/BUILD.txt</code> for the commit SHA).
</div>

<h2>3. Paste your token on first run</h2>
<p>When you launch the agent for the first time, it will prompt for your bearer token. Paste the one above; the agent persists it locally and the network knows you're online.</p>

<div class="small">
  Power user / not on Windows? You can also call the API directly with
  <code>Authorization: Bearer &lt;token&gt;</code> against
  <code>https://ai.dallinlayton.com</code>, or grab the standalone
  <a href="/download/agent.exe">agent.exe</a> and pair it with a custom
  <code>config.json</code>.
</div>

<script>
const btn = document.getElementById('copy');
const tok = document.getElementById('tok');
btn.addEventListener('click', async () => {{
  try {{
    await navigator.clipboard.writeText(tok.textContent.trim());
    btn.textContent = 'Copied ✓';
    btn.classList.add('ok');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('ok'); }}, 2000);
  }} catch (e) {{
    // Fallback: select the text so the user can copy with their keyboard.
    const r = document.createRange();
    r.selectNode(tok);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
    btn.textContent = 'Selected — Ctrl+C';
  }}
}});
</script>
</body></html>
"""

_REDEEM_ERROR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Invite unavailable — GamerAI</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
  h1{{color:#b00020}}
</style></head>
<body>
<h1>This invite isn't available</h1>
<p>{detail}</p>
<p>Ask the person who sent the invite for a fresh link.</p>
</body></html>
"""


def _render_redeem_page(details: dict) -> str:
    contributor = (
        details.get("contributor_email")
        or details.get("contributor_member_id")
        or "a GamerAI contributor"
    )
    cap = details.get("daily_quota_tokens")
    cap_text = f"{cap} tokens/day" if cap else "unlimited"
    expires_at = details.get("expires_at")
    if expires_at:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        expiry_block = f'<div class="label">Expires</div><div class="value">{html_lib.escape(when)}</div>'
    else:
        expiry_block = ""
    return _REDEEM_PAGE.format(
        contributor=html_lib.escape(str(contributor)),
        cap=html_lib.escape(cap_text),
        expiry_block=expiry_block,
    )


@app.get("/invite/{code}", response_class=HTMLResponse)
async def invite_landing(code: str):
    """Public landing page Bob hits when he clicks Alice's invite URL."""
    async with _public_client() as c:
        r = await c.get(f"/invites/{code}", timeout=5)
    if r.status_code == 404:
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(detail="The invite code was not found."),
            status_code=404,
        )
    if r.status_code >= 400:
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(
                detail=f"The coordinator returned status {r.status_code}."
            ),
            status_code=r.status_code,
        )
    details = r.json()
    state = details.get("state")
    if state and state != "open":
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(
                detail=f"This invite is {html_lib.escape(state)}."
            ),
            status_code=410,
        )
    return HTMLResponse(_render_redeem_page(details))


@app.post("/invite/{code}", response_class=HTMLResponse)
async def invite_accept(
    code: str,
    invitee_email: str = Form(default=""),
    tos_accepted: str = Form(default=""),
):
    """Bob hits Accept. Mint a member token through the coordinator.
    The ToS checkbox is required client-side (HTML5 ``required``) AND
    server-side (coordinator rejects requests without ``tos_accepted``).
    Belt-and-suspenders: a savvy user could remove the ``required``
    attribute via devtools, but the coordinator still refuses."""
    if tos_accepted != "on":
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(
                detail=(
                    "The community terms must be accepted to redeem this "
                    "invite. Go back, check the box, and try again."
                ),
            ),
            status_code=400,
        )
    body = {
        "invitee_email": invitee_email.strip() or None,
        "tos_accepted": True,
    }
    async with _public_client() as c:
        r = await c.post(f"/invites/{code}/accept", json=body, timeout=5)
    if r.status_code == 404:
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(detail="The invite code was not found."),
            status_code=404,
        )
    if r.status_code == 410:
        detail = r.json().get("detail", "no longer redeemable")
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(
                detail=f"This invite is {html_lib.escape(detail)}."
            ),
            status_code=410,
        )
    if r.status_code >= 400:
        return HTMLResponse(
            _REDEEM_ERROR_PAGE.format(
                detail=f"Accept failed (status {r.status_code}). {html_lib.escape(r.text[:300])}"
            ),
            status_code=r.status_code,
        )
    body_json = r.json()
    cap = body_json.get("daily_quota_tokens")
    cap_text = str(cap) if cap else "unlimited"
    return HTMLResponse(
        _REDEEM_DONE_PAGE.format(
            token=html_lib.escape(body_json["token"]),
            member_id=html_lib.escape(body_json["member_id"]),
            cap=html_lib.escape(cap_text),
        )
    )


# ---------- admin pages ----------
async def _require_admin_session(request: Request):
    """Used by the admin HTML pages. Returns the session bearer when
    the caller is an authenticated admin; redirects to /login when no
    session; returns a 403 HTML page otherwise."""
    bearer = _session_bearer(request)
    me = await _identify(bearer) if bearer else None
    if me is None:
        return None, _login_redirect(str(request.url.path))
    if me.get("role") != "admin":
        return None, HTMLResponse(
            "<h1>403 — admin only</h1>"
            '<p><a href="/">Back to chat</a></p>',
            status_code=403,
        )
    return bearer, None


@app.get("/admin/members", response_class=HTMLResponse)
async def admin_members(request: Request):
    """Admin-only table of every member. Session cookie gate added in
    the browser-auth slice; previously this page used the admin API
    token and was kept off the public domain via Caddy."""
    bearer, fail = await _require_admin_session(request)
    if fail is not None:
        return fail
    async with _client(bearer=bearer) as c:
        r = await c.get("/admin/members", timeout=5)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    members = r.json()["members"]
    rows = "".join(
        f"<tr>"
        f"<td><code>{html_lib.escape(m['member_id'])}</code></td>"
        f"<td>{html_lib.escape(m['role'])}</td>"
        f"<td>{html_lib.escape(m['tier'])}</td>"
        f"<td>{html_lib.escape(m['email'] or '-')}</td>"
        f"<td><code>{html_lib.escape(m['parent_member_id'] or '-')}</code></td>"
        f"<td>{m['daily_quota_tokens'] if m['daily_quota_tokens'] is not None else '∞'}</td>"
        f"<td>{'yes' if m['revoked_at'] else 'no'}</td>"
        f"</tr>"
        for m in members
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>GamerAI — members</title>
<style>
  body{{font-family:system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}
  h1{{margin-bottom:.5rem}}
  table{{width:100%;border-collapse:collapse;font-size:.9rem}}
  th,td{{padding:.5rem;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
  th{{background:#f5f5f5}}
  code{{font-size:.85em}}
  a{{color:#2d6cdf;text-decoration:none}}
</style></head>
<body>
<h1>Members ({len(members)})</h1>
<p><a href="/admin/invites">→ invites</a> · <a href="/dashboard">← dashboard</a></p>
<table>
<tr><th>member_id</th><th>role</th><th>tier</th><th>email</th><th>parent</th><th>quota/day</th><th>revoked</th></tr>
{rows}
</table>
</body></html>"""
    return HTMLResponse(html)


@app.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites(request: Request):
    """Admin view of every invite in the system, redeemed or not."""
    bearer, fail = await _require_admin_session(request)
    if fail is not None:
        return fail
    async with _client(bearer=bearer) as c:
        r = await c.get("/invites?all=true", timeout=5)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    invites = r.json()["invites"]
    rows = "".join(
        f"<tr>"
        f"<td><code>{html_lib.escape(i['code'])}</code></td>"
        f"<td>{html_lib.escape(i.get('state', '?'))}</td>"
        f"<td><code>{html_lib.escape(i['contributor_member_id'])}</code></td>"
        f"<td>{html_lib.escape(i['invitee_email'] or '-')}</td>"
        f"<td>{i['daily_quota_tokens'] if i['daily_quota_tokens'] is not None else '∞'}</td>"
        f"<td><code>{html_lib.escape(i['accepted_by_member_id'] or '-')}</code></td>"
        f"</tr>"
        for i in invites
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>GamerAI — invites</title>
<style>
  body{{font-family:system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}
  h1{{margin-bottom:.5rem}}
  table{{width:100%;border-collapse:collapse;font-size:.9rem}}
  th,td{{padding:.5rem;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
  th{{background:#f5f5f5}}
  code{{font-size:.85em}}
  a{{color:#2d6cdf;text-decoration:none}}
</style></head>
<body>
<h1>Invites ({len(invites)})</h1>
<p><a href="/admin/members">→ members</a> · <a href="/dashboard">← dashboard</a></p>
<table>
<tr><th>code</th><th>state</th><th>contributor</th><th>invitee email</th><th>quota/day</th><th>accepted by</th></tr>
{rows}
</table>
</body></html>"""
    return HTMLResponse(html)


# ---------- proxy endpoints (avoids CORS for browser) ----------
# All /api/* proxies use the caller's session-cookie bearer so the
# coordinator authenticates the prompt as the logged-in member. A
# request with no session is rejected 401 — the JS in INDEX_HTML
# will see that and redirect.
def _require_session_bearer(request: Request) -> str:
    bearer = _session_bearer(request)
    if not bearer:
        raise HTTPException(status_code=401, detail="not signed in")
    return bearer


@app.post("/api/generate")
async def proxy_generate(payload: dict, request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.post("/generate", json=payload, timeout=10)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return JSONResponse(r.json())


@app.get("/api/result/{job_id}")
async def proxy_result(job_id: str, request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.get(f"/result/{job_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/me")
async def proxy_me(request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.get("/me", timeout=5)
    return JSONResponse(r.json(), status_code=r.status_code)


# Workers / earnings / metrics are operational data — admin only.
async def _admin_only_proxy(request: Request, path: str):
    bearer = _require_session_bearer(request)
    me = await _identify(bearer)
    if me is None or me.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    async with _client(bearer=bearer) as c:
        r = await c.get(path, timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/workers")
async def proxy_workers(request: Request):
    return await _admin_only_proxy(request, "/workers")


@app.get("/api/earnings")
async def proxy_earnings(request: Request):
    return await _admin_only_proxy(request, "/earnings")


@app.get("/api/metrics")
async def proxy_metrics(request: Request):
    return await _admin_only_proxy(request, "/metrics")


@app.get("/api/conversations")
async def proxy_list_conversations(request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.get("/conversations", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/conversations")
async def proxy_create_conversation(payload: dict, request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.post("/conversations", json=payload, timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/conversations/{conversation_id}")
async def proxy_get_conversation(conversation_id: str, request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.get(f"/conversations/{conversation_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/api/conversations/{conversation_id}")
async def proxy_archive_conversation(conversation_id: str, request: Request):
    bearer = _require_session_bearer(request)
    async with _client(bearer=bearer) as c:
        r = await c.delete(f"/conversations/{conversation_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)
