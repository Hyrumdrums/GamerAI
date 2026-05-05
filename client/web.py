"""Tiny FastAPI web UI for GamerAI. Submit prompts, browse workers/earnings/metrics."""
import os

import httpx
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from shared.auth import auth_headers

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://coordinator:8000")

app = FastAPI(title="GamerAI Web UI")


def _client() -> httpx.AsyncClient:
    """httpx client preconfigured with the coordinator base URL and any
    bearer-token auth headers (no-op when API_TOKEN is unset)."""
    return httpx.AsyncClient(base_url=COORDINATOR_URL, headers=auth_headers())

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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    async with _client() as c:
        m = (await c.get("/metrics", timeout=5)).json()
        w = (await c.get("/workers", timeout=5)).json()
    rows = "".join(
        f"<tr><td>{x['worker_id']}</td><td>{x['status']}</td>"
        f"<td>{x['total_jobs']}</td><td>{x['total_tokens']}</td>"
        f"<td>${x['total_usd']}</td></tr>"
        for x in w["workers"]
    )
    metrics_html = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in m.items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>GamerAI dashboard</title>
<style>body{{font-family:system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse;font-size:.9rem;margin-bottom:2rem}}
th,td{{padding:.4rem .6rem;border-bottom:1px solid #eee;text-align:left}}
th{{background:#fafafa}}a{{color:#2d6cdf}}</style></head>
<body><h1>Dashboard</h1><p><a href="/">&larr; back</a></p>
<h2>Metrics</h2><table><tr><th>metric</th><th>value</th></tr>{metrics_html}</table>
<h2>Workers</h2><table><tr><th>worker</th><th>status</th><th>jobs</th><th>tokens</th><th>usd</th></tr>{rows}</table>
</body></html>"""


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
