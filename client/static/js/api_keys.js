// Copy-to-clipboard with a visible-selection fallback for insecure
// contexts / denied permission — shared by every copy-* button below.
async function copyText(btn, value, promptLabel) {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    const prev = btn.textContent;
    btn.textContent = 'copied!';
    setTimeout(() => { btn.textContent = prev; }, 1200);
  } catch {
    window.prompt(promptLabel, value);
  }
}

// "Copy API key" button — copies a raw opaque value (not a relative href
// resolved to an absolute URL, the way account.js's copy-link button
// works — that would mangle a bearer token).
document.querySelectorAll('button.copy-value').forEach((btn) => {
  btn.addEventListener('click', () => copyText(btn, btn.dataset.value, 'Copy this API key:'));
});

// The "for your AI agent" setup spec. Built client-side (not server-
// rendered) so the base URL is always the origin that actually served
// this page — correct in prod, staging, or local dev without needing a
// PUBLIC_BASE_URL env lookup, same reasoning as account.js's copy-link
// resolving against window.location.origin.
const specEl = document.getElementById('agent-spec');
if (specEl) {
  const origin = window.location.origin;
  const key = specEl.dataset.key || '<YOUR_API_KEY>';
  specEl.textContent = `GamerAI — OpenAI-compatible API setup

Base URL:  ${origin}
API key:   ${key}
Auth:      Authorization: Bearer <API key>

Endpoints:
  GET  /v1/models              list available chat models
  POST /v1/chat/completions    OpenAI-compatible chat completions
                                (set "stream": true for SSE)

Example:
  curl ${origin}/v1/chat/completions \\
    -H "Authorization: Bearer ${key}" \\
    -H "Content-Type: application/json" \\
    -d '{"messages":[{"role":"user","content":"hello"}]}'

Notes for whoever configures this:
  - "model" is optional — omit it to get the network's default chat
    model, or call GET /v1/models first to pick one by name.
  - This key can only generate chat completions — it cannot manage
    the account, invites, billing, or other machines.
  - Requests run on community-contributed GPUs, not a dedicated
    server: expect more latency variance than a typical paid API, and
    occasionally a 503 if no contributor machine is online right now.
  - Daily usage is capped by the account's tier; going over returns
    429 with an OpenAI-shaped {"error": {...}} body.
  - temperature / max_tokens are accepted but currently ignored.

Task: configure this as the backend for [Home Assistant's "OpenAI
Conversation" integration | Open WebUI's OpenAI connection | the tool
I'm using], using the base URL and key above.`;

  const copyBtn = document.querySelector('button.copy-spec');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => copyText(copyBtn, specEl.textContent, 'Copy this setup spec:'));
  }
}
