"""Tiny FastAPI web UI for GamerAI. Submit prompts, browse workers/earnings/metrics."""
import html as html_lib
import os

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared.auth import auth_headers

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")

app = FastAPI(title="GamerAI Web UI")


def _client() -> httpx.AsyncClient:
    """httpx client preconfigured with the coordinator base URL and any
    bearer-token auth headers (no-op when API_TOKEN is unset)."""
    return httpx.AsyncClient(base_url=COORDINATOR_URL, headers=auth_headers())


def _public_client() -> httpx.AsyncClient:
    """httpx client for the public invite-redemption flow. Sends no
    Authorization header — the invite code itself is the credential."""
    return httpx.AsyncClient(base_url=COORDINATOR_URL)

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>GamerAI</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:780px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
  h1{margin-bottom:.25rem}
  .sub{color:#666;margin-bottom:2rem}
  textarea{width:100%;min-height:6rem;font-size:1rem;padding:.6rem;box-sizing:border-box}
  button{font-size:1rem;padding:.5rem 1.2rem;cursor:pointer;background:#2d6cdf;color:#fff;border:0;border-radius:4px}
  button:hover{background:#1f55b8}
  pre{background:#f5f5f5;padding:1rem;white-space:pre-wrap;word-break:break-word;border-radius:4px}
  .row{display:flex;gap:.5rem;margin-top:.5rem;flex-wrap:wrap}
  .row a{font-size:.9rem;color:#2d6cdf;text-decoration:none}
  .meta{color:#666;font-size:.85rem;margin-top:.5rem}
  table{width:100%;border-collapse:collapse;margin-top:1rem;font-size:.9rem}
  th,td{padding:.4rem .6rem;border-bottom:1px solid #eee;text-align:left}
  th{background:#fafafa}
</style></head>
<body>
<h1>GamerAI</h1>
<div class="sub">Distributed inference, paid per token.</div>

<form id="f">
  <textarea id="prompt" placeholder="Ask anything..."></textarea>
  <div class="row">
    <button type="submit">Submit</button>
    <a href="/dashboard">dashboard</a>
    <a href="/api/workers" target="_blank">/workers</a>
    <a href="/api/earnings" target="_blank">/earnings</a>
    <a href="/api/metrics" target="_blank">/metrics</a>
  </div>
</form>

<div id="status" class="meta"></div>
<pre id="out" hidden></pre>

<script>
const f = document.getElementById('f');
const out = document.getElementById('out');
const status = document.getElementById('status');
f.onsubmit = async (e) => {
  e.preventDefault();
  const prompt = document.getElementById('prompt').value.trim();
  if (!prompt) return;
  out.hidden = true; out.textContent = '';
  status.textContent = 'submitting...';
  const r = await fetch('/api/generate', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({prompt})
  });
  const {job_id} = await r.json();
  status.textContent = 'job '+job_id+' — running...';
  const start = Date.now();
  while (true) {
    await new Promise(r => setTimeout(r, 1000));
    const res = await fetch('/api/result/'+job_id).then(r=>r.json());
    if (res.status === 'complete' || res.status === 'error') {
      out.hidden = false;
      out.textContent = res.text || res.error || JSON.stringify(res, null, 2);
      const dt = ((Date.now()-start)/1000).toFixed(1);
      status.textContent = `done in ${dt}s — worker ${res.worker_id} — ${res.completion_tokens} tokens — $${res.earnings}`;
      return;
    }
  }
};
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/admin")
def admin_redirect():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    async with _client() as c:
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
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
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

<form method="POST">
  <label for="email" class="label">Your email (optional, helps the inviter recognize you)</label>
  <input id="email" name="invitee_email" type="email" placeholder="you@example.com" />
  <button type="submit">Accept and get my token</button>
</form>
</body></html>
"""

_REDEEM_DONE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Welcome to GamerAI</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
  h1{{margin-bottom:.25rem}}
  .sub{{color:#666;margin-bottom:1.5rem}}
  .token{{font-family:ui-monospace,Menlo,Consolas,monospace;background:#fff8c4;padding:.75rem;border-radius:4px;font-size:1.1rem;word-break:break-all;border:1px solid #ddc97a}}
  .warn{{color:#b06000;font-weight:600;margin:1rem 0 .5rem}}
  .small{{color:#666;font-size:.9rem;margin-top:1rem}}
</style></head>
<body>
<h1>Welcome to GamerAI</h1>
<div class="sub">Your invitee account is live. Save this token now — it cannot be recovered.</div>

<div class="warn">Your bearer token</div>
<div class="token">{token}</div>

<div class="small">
  <strong>How to use it:</strong> add it as your <code>Authorization: Bearer &lt;token&gt;</code>
  header when you call the GamerAI API, or paste it into the client config of your choice.
  <br><br>
  member_id: <code>{member_id}</code><br>
  daily_quota_tokens: <code>{cap}</code>
</div>
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
async def invite_accept(code: str, invitee_email: str = Form(default="")):
    """Bob hits Accept. Mint a member token through the coordinator."""
    body = {"invitee_email": invitee_email.strip() or None}
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
@app.get("/admin/members", response_class=HTMLResponse)
async def admin_members():
    """Admin-only table of every member. The web UI talks to the
    coordinator as the admin (it inherits API_TOKEN); restrict access
    to this page via Caddy basic_auth or an SSH tunnel for now."""
    async with _client() as c:
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
async def admin_invites():
    """Admin view of every invite in the system, redeemed or not."""
    async with _client() as c:
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
@app.post("/api/generate")
async def proxy_generate(payload: dict):
    async with _client() as c:
        r = await c.post("/generate", json=payload, timeout=10)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return JSONResponse(r.json())


@app.get("/api/result/{job_id}")
async def proxy_result(job_id: str):
    async with _client() as c:
        r = await c.get(f"/result/{job_id}", timeout=10)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/workers")
async def proxy_workers():
    async with _client() as c:
        r = await c.get("/workers", timeout=10)
    return JSONResponse(r.json())


@app.get("/api/earnings")
async def proxy_earnings():
    async with _client() as c:
        r = await c.get("/earnings", timeout=10)
    return JSONResponse(r.json())


@app.get("/api/metrics")
async def proxy_metrics():
    async with _client() as c:
        r = await c.get("/metrics", timeout=10)
    return JSONResponse(r.json())
