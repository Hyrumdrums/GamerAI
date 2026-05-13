# Developer log

Chronological record of meaningful changes, decisions, and gotchas. Most-recent
entries on top. Skim for context before resuming work.

---

## 2026-05-12 — ChatGPT-shaped UI: conversations, browser auth, chat rewrite

Three connected slices that turn the single-prompt-and-result form
into something a stranger could land on and actually use. Each was
independently shippable; bundling them in one devlog entry because
they only make sense together.

### 1. Multi-turn conversation API (coordinator)

The data layer for threads:

- New tables: `conversations` (id, owner_member_id, title, model,
  created_at, updated_at, archived_at) and `messages` (id,
  conversation_id, seq, role, text, job_id, model, tokens). Messages
  stack via `seq`; assistant rows link back to the originating
  `jobs.job_id` for forensics.
- Additive migration: `jobs.conversation_id`.
- Endpoints:
  - `POST /conversations` — create
  - `GET /conversations` — caller's threads, most-recently-updated
    first; archived hidden unless `?include_archived=true`
  - `GET /conversations/<id>` — thread with all messages
  - `DELETE /conversations/<id>` — soft archive

- `/generate` now accepts `conversation_id`. When set, the coordinator
  loads prior turns, concatenates them with `User:`/`Assistant:`
  prefixes into the worker-facing prompt, then stamps the
  conversation_id onto the jobs row. The original (un-prepended)
  user prompt is stored separately so `/jobs/complete` can replay
  *only the new turn* into the conversation history. Result: the
  worker contract is unchanged; multi-turn lives entirely on the
  coordinator side.
- `/jobs/complete` auto-appends two rows on completion when the job
  was tied to a conversation: the user message (the original prompt)
  and the assistant response. Sets the conversation title from the
  first user message (idempotent — only when title is still NULL).
- Ownership enforced everywhere: a member can only read/write their
  own conversations. Admin can read any (for moderation). 404 on
  cross-member access — deliberately the same as missing, to avoid
  leaking existence.

13 new tests in `tests/test_conversations.py`.

### 2. Browser auth on the web UI

The chat UI was previously submitting every prompt as the *admin* —
`client/web.py`'s `_client()` used the API_TOKEN env var for every
coordinator call. That meant the chat box couldn't safely go public
because anyone hitting it would inherit admin permissions. This slice
fixes that:

- `_client(bearer=...)` accepts an optional per-request bearer; falls
  back to admin only on legacy paths (admin browser views).
- New session cookie: `gai_session`. The cookie value IS the user's
  bearer token. `HttpOnly` prevents JS read, `Secure` keeps it off
  plain HTTP, `SameSite=lax` handles CSRF. 30-day Max-Age.
- `/login` (GET) shows a paste-your-token form. POST validates the
  token by calling `/me` against the coordinator; on success the
  cookie is set and the user is redirected. Invalid token re-renders
  the form with an error (401).
- `/logout` clears the cookie.
- Every `/api/*` proxy now reads the session cookie's bearer and
  forwards it as `Authorization: Bearer …`. Workers/earnings/metrics
  proxies are admin-gated (403 non-admin). Generate/result/me/
  conversations are open to any authenticated member.
- Admin HTML pages (`/dashboard`, `/admin/members`, `/admin/invites`)
  now require role=admin from the session, not just inside-the-SSH-
  tunnel.
- Caddy: `/`, `/login`, `/logout`, `/api/*`, `/dashboard`,
  `/admin/*` now forward to the web UI publicly. Session-cookie auth
  is the gate. Invite redemption stays untouched-public.

7 new tests in `tests/test_web_ui_smoke.py`.

### 3. Conversation-aware chat UI

`INDEX_HTML` rewritten from a single-prompt form to a real chat
shape:

- Two-column layout: left sidebar lists conversations (titles auto-
  derived from each thread's first user message); main pane shows
  message bubbles for the active conversation. Sidebar entries
  highlight on click; "+ New chat" button starts a fresh thread.
- Composer auto-grows up to 12 rows, submits on Enter (Shift+Enter
  for newline), disables Send while in-flight.
- Optimistic UI: as soon as the user submits, the user message
  bubble appears immediately followed by a "thinking…" placeholder.
  The placeholder is replaced by the assistant response when the
  job completes.
- First submit on a brand-new chat auto-creates the conversation,
  then submits with `conversation_id` set.
- Markdown rendering for assistant messages via `marked.min.js` from
  the jsDelivr CDN. User messages render literal (don't surprise the
  user with markdown rendering of code they pasted).
- Sidebar refreshes after each completion so the title (set from the
  first prompt by the coordinator) shows up without a manual reload.
- Status line under the composer shows job duration + token count
  per turn.

One new test (`/api/conversations` round-trip via the web UI proxy);
existing index-page test updated to assert the new shell elements.

### What you'll see at https://ai.dallinlayton.com/

```
┌─────────────┬────────────────────────────────────┐
│ + New chat  │ GamerAI         hyrumdrums  terms… │
├─────────────┼────────────────────────────────────┤
│ ▎ Why is    │  USER     Why is the sky blue?     │
│   the sky.. │                                    │
│             │  ASSIST   The sky appears blue     │
│   What's    │           because of Rayleigh      │
│   the cap.. │           scattering...            │
│             │                                    │
│             ├────────────────────────────────────┤
│             │ [ Message GamerAI… ]  [Send]       │
│             │ done in 6.9s · 44 tokens           │
└─────────────┴────────────────────────────────────┘
```

Click into "Why is the sky blue", paste a follow-up like "And why
not green?" — the coordinator prepends the prior turn, the worker
serves a context-aware response, both turns persist.

### Test scoreboard

- 158/158 passing (137 → 150 with conversations slice → 157 with
  browser-auth slice → 158 with chat-UI slice).

### What's deliberately NOT in this push

- **Streaming.** Responses still arrive all-at-once after polling
  every 1s. Token-streaming via SSE on the coordinator + an Ollama
  `/api/chat`-style streaming worker contract is its own slice.
- **Conversation history search.** No "find a chat about Python
  decorators." Defer until per-user volume justifies an index. The
  Phase 3b.iii encrypted-history-at-rest design also constrains
  what server-side search can do.
- **Conversation rename / delete buttons in the UI.** The API
  supports archive via `DELETE /conversations/<id>` but there's no
  UI affordance yet.
- **Stop generation button.** Today, once you submit, you wait. A
  user-cancel button would require coordinator-side abandon
  routing for partially-completed jobs.
- **Multi-model picker.** Every prompt still goes to `llama3.2:1b`
  (the only model anyone has). Will become useful once we have
  multiple worker model classes.
- **Mobile responsiveness.** Two-column desktop layout. Sidebar will
  break on narrow screens. Defer.

### File layout changes

```
coordinator/db.py              <- conversations + messages tables,
                                  jobs.conversation_id migration,
                                  CRUD + append/touch + title helper
coordinator/main.py            <- /conversations endpoints,
                                  conversation-aware /generate,
                                  auto-append in /jobs/complete,
                                  _format_chat_prompt helper
shared/models.py               <- GenerateRequest.conversation_id,
                                  new ConversationCreateRequest
client/web.py                  <- session cookie helpers, /login,
                                  /logout, INDEX_HTML rewrite,
                                  per-session /api/* proxies,
                                  admin-gated /api/workers/earnings/metrics
infra/Caddyfile                <- /, /login, /logout, /api/*,
                                  /dashboard, /admin/* exposed publicly
infra/docker-compose.prod.yml  <- PUBLIC_BASE_URL on client container
                                  for Secure-cookie detection
tests/test_conversations.py    <- new (13 tests)
tests/test_web_ui_smoke.py     <- +8 tests for login/session/conv-proxy
```

### Deploy steps for the VPS

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.235.139
cd /opt/gamerai
git pull
sudo /opt/gamerai/infra/deploy.sh
docker restart gamerai-caddy   # for the new /login etc. routes
```

After that, `https://ai.dallinlayton.com/` is the public chat. Paste
your bearer at `/login` and start.

---

## 2026-05-12 — First real prompt served by a real contributor's GPU

The 2026-05-11 invite slice closed the recruitment loop on paper.
Earlier today the bootstrap slice closed it on a real machine. This
afternoon we ran the actual experiment the project has been chasing
since the very first commit: **prompt submitted from a different
machine, queued by the coordinator, claimed and served by a real
contributor's GPU, response returned to the original submitter.**

### What the test looked like

```bash
ssh root@5.161.235.139 'docker stop gamerai-worker-1'   # silence in-VPS mock

curl -X POST https://ai.dallinlayton.com/generate \\
  -H "Authorization: Bearer $API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "In one sentence, why is the sky blue?"}'
# → {"job_id": "84653df9-..."}

# poll /result/<id>...
# → status: complete after ~7s
```

### The result row that mattered

```json
{
    "job_id": "84653df9-f12f-401a-aa2d-acd3d0baa21f",
    "status": "complete",
    "worker_id": "win-Home-PC-d4f76ae1",
    "model": "llama3.2:1b",
    "text": "The sky appears blue because of a phenomenon called
             Rayleigh scattering, in which shorter (blue) wavelengths
             of light are scattered more than longer (red) wavelengths
             by the tiny molecules of gases in the Earth's atmosphere.",
    "prompt_tokens": 35,
    "completion_tokens": 44,
    "earnings": 0.000154,
    "duration_seconds": 6.934
}
```

`worker_id: win-Home-PC-d4f76ae1` is the founder's Windows box.
`model: llama3.2:1b` is the bundled default. `text` is a coherent
non-mock answer. 6.9s end-to-end includes ~2s of polling overhead,
so raw inference was probably ~3s on the Windows GPU.

VPS mock worker restarted after the test; the network is back to
its multi-worker steady state.

### Why this matters

Every prior `/generate` on the public coordinator was served by the
in-VPS mock worker. Every contributor demo before today was the
founder's own box talking to the founder's own coordinator. This is
the first time the request and the GPU were on different machines
under the production routing path — same loop a stranger's request
would take through a stranger's machine.

The platform is now structurally ready to serve a real community.
What it doesn't have yet — and what blocks recruiting beyond the
founder's circle — is the trust + safety layer for when "the
contributor isn't you." That's the next slice (see entry below).

---

## 2026-05-12 — Community trust: ToS + canary integrity monitoring

The 2026-05-11 invite slice and today's first-real-prompt result make
the network real on paper. They don't make it safe to recruit
strangers. Two missing pieces blocked that:

1. **No social contract.** No written ToS for contributors or invitees
   to agree to. No place to set expectations like "prompts are
   visible to the GPU that serves them — don't paste secrets" or
   "run unmodified models."
2. **No detection layer.** A malicious contributor could swap in a
   finetuned/backdoored model and the coordinator would have no
   way to notice. Outputs would look plausible but be subtly wrong
   (or poisoned).

This slice ships both:

### ToS

- `docs/community-tos.md` — plain-English terms covering: prompt
  visibility, no-secrets warning, contributor honesty (run the model
  you said you'd run), monitoring disclosure (canaries + agreement
  scoring exist and aren't surveillance), best-effort service / no
  warranty, jurisdiction.
- Coordinator endpoints:
  - `GET /tos` — public HTML, version-stamped at the top.
  - `GET /tos/raw` — public markdown + `X-Tos-Version` header.
- Redemption page rewritten: TL;DR box, "Read full terms" link,
  required checkbox, "Accept" button disabled until checked.
- Belt and suspenders: server-side check too. `POST /invites/<code>/accept`
  returns 400 if `tos_accepted` isn't `true`, so a savvy user who
  pops devtools to remove the HTML5 `required` attribute still gets
  bounced.
- The accepted version is stamped onto the member row
  (`members.tos_accepted_at`, `members.tos_version`). `/me` exposes
  `needs_reaccept` so a future ToS bump can prompt existing members
  to re-accept.

### Canaries

- New tables: `canaries` (prompt + required_tokens + model) and
  `canary_results` (per-completion pass/fail audit).
- Admin CLI: `python -m coordinator.admin seed-canaries` installs the
  default factual-answer list (Earth / 1969 / Pacific / H2O / 2+3).
- Coordinator background thread `CanaryInjector` wakes every
  `CANARY_INTERVAL_SECONDS` (default 600s = 10 min) and pushes one
  canary into the job queue. **The worker sees nothing distinguishing
  it from a real prompt** — that mapping (job_id → canary_id) lives
  in Redis only, on the coordinator side.
- `/jobs/complete` recognizes canary completions: it verifies the
  response contains every required token (case-insensitive substring),
  records the result, and intentionally skips both earnings credit
  and member-usage rollup. The worker is told `{"ok": true,
  "earnings": 0.0}` either way, so a worker that special-cased the
  zero-earnings reply to detect canaries would also reveal itself
  by handling that case at all.

### Agreement scoring (first cut)

- `canary_score_for_worker(worker_id, limit=50)` returns
  `{passed, total, score}` over the most recent window. Surfaced on
  `GET /workers` as `canary_score`.
- This is the v1 of "agreement" — agreement-with-known-good-answer,
  not agreement-with-other-workers. Worker-vs-worker k-of-n consensus
  on real prompts is the next layer (requires fan-out routing +
  output comparison + payout queuing). Deferred until the network has
  enough workers to make consensus meaningful.

### Why canaries before consensus

Cost vs. value:
- Canary infra is small (one background thread + a check in /jobs/complete
  + a score query). Catches the biggest class of attack (substituted
  model returning systematically different answers).
- Consensus infra is large (dispatch picks 2+ workers, both responses
  collected, similarity threshold, payout held until verified) and
  only adds value when we have many contributors to compare.
- At 3 contributors, canaries are sufficient. Past 10–20, consensus
  becomes worthwhile.

### What we deliberately did NOT do

- **Prompt encryption / TEE / FHE.** Out-of-scope for community-tier
  contributors. The ToS is honest about prompt visibility; users who
  need confidentiality should wait for the Phase 5 client-side
  embedding tier.
- **Signed Ollama installer or attestable model hash.** A malicious
  contributor could still run a modified Ollama; the canary system
  detects them behaviorally rather than cryptographically. Real
  attestation requires hardware that excludes most of the contributor
  pool.
- **Auto-quarantine of failing workers.** A worker that fails 5/5
  canaries gets a score of 0.0 in `/workers`, but they keep
  receiving jobs. Manual admin review for now; auto-suspension can
  ship once the score has been calibrated in production.
- **Per-tier ToS bumps.** A bumped `TOS_VERSION` will surface as
  `needs_reaccept: true` on `/me`, but nothing yet blocks an
  existing member from continuing to use the network. That's a
  policy decision (do we hard-block? soft-nudge?) we'll make when
  we actually have a substantive ToS change.

### Test scoreboard

- 137/137 passing (123 prior + 14 new in
  `tests/test_tos_and_canaries.py`: ToS endpoint coverage, invite
  accept enforcement, canary verify function, end-to-end
  inject→complete→score path).
- Existing invite tests updated to send `tos_accepted=true`.

### File layout changes

```
docs/community-tos.md          <- new; the canonical terms doc
coordinator/canaries.py        <- new; CanaryInjector + verify_response
coordinator/db.py              <- canaries + canary_results tables,
                                  tos_accepted_at + tos_version
                                  columns on members, canary CRUD,
                                  canary_score_for_worker()
coordinator/main.py            <- /tos + /tos/raw endpoints,
                                  ToS enforcement on /invites/<>/accept,
                                  canary detection in /jobs/complete,
                                  canary_score on /workers,
                                  CanaryInjector wired into lifespan
coordinator/admin.py           <- seed-canaries CLI subcommand
coordinator/member_auth.py     <- Member dataclass gets tos fields
shared/config.py               <- CANARY_INTERVAL_SECONDS, CANARY_PENDING
shared/models.py               <- InviteAcceptRequest.tos_accepted
client/web.py                  <- redemption page TL;DR + checkbox
tests/test_tos_and_canaries.py <- new
tests/test_web_ui_smoke.py     <- updated for tos_accepted contract
tests/test_member_auth_e2e.py  <- updated for tos_accepted contract
```

### Deploy steps for the VPS

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.235.139
cd /opt/gamerai
git pull
sudo /opt/gamerai/infra/deploy.sh
docker restart gamerai-caddy        # picks up the filename header from earlier slice
docker exec -it gamerai-coordinator python -m coordinator.admin seed-canaries
```

After that, the first canary will hit the queue within 10 minutes
(plus the random initial-delay stagger). If a contributor is online
and idle, they'll claim it and the coordinator will tick their
score. `curl https://ai.dallinlayton.com/workers -H "Authorization:
Bearer $API_TOKEN"` shows the new `canary_score` field per worker.

---

## 2026-05-12 — Inference bootstrap (Ollama + default model on first run)

The previous slices delivered an installer, an invite flow, a token
prompt, keep-awake, drain visibility, and self-update — every layer of
the contributor experience *except* the part that actually matters:
real inference. Until today a fresh-install contributor's machine
would register, claim jobs, and return **mock output**, because the
agent only calls Ollama when `OLLAMA_URL` is set — and a non-developer
recruit never sets that. This slice closes the gap: the agent installs
Ollama and the default model on first run, from a mirror we control.

### What ships

```
fresh Windows box                            ai.dallinlayton.com
─────────────────                            ───────────────────
agent.exe first launch                       /download/ollama-setup.exe
  ↓                                          /download/models/llama3.2-1b.gguf
probe localhost:11434 → 404                  /download/models/llama3.2-1b.Modelfile
  ↓
find ollama.exe → none
  ↓
download ollama-setup.exe ◄──────────────────
  ↓
run /S (silent install)
  ↓
wait for 11434 (up to 60s)
  ↓
GET /api/tags → no llama3.2:1b
  ↓
HEAD mirror gguf → 200
  ↓
download .gguf + Modelfile  ◄────────────────
  ↓
rewrite FROM to absolute path
  ↓
ollama create llama3.2:1b -f Modelfile
  ↓
set OLLAMA_URL=http://localhost:11434
  ↓
register({"models": ["llama3.2:1b"]})
  ↓
main_loop runs REAL inference
```

Best-effort throughout: any step failing leaves the agent running with
mock inference (same as before this slice). Idempotent: every step is
a fast no-op when its precondition is already met, so subsequent
launches probe → "ollama already running" → "model already installed"
→ done in <500ms.

### Mirror-first, ollama-pull fallback

The user pushed back when I waved my hand at "just `ollama pull`":
the whole point of building infrastructure is that we control the
supply chain. So the agent tries our mirror first:

1. `HEAD {mirror}/download/models/{slug}.gguf` — if 200, download both
   the `.gguf` and a sibling `.Modelfile`, rewrite the relative `FROM`
   to an absolute path, run `ollama create`.
2. If the mirror returns anything other than 200, fall back to
   `POST {ollama_url}/api/pull` (streams from Ollama's CDN).

This way the mirror is authoritative when populated; Ollama's CDN is
the dependable fallback so a contributor whose model isn't on our
mirror yet still gets a working agent. When we eventually publish a
fine-tuned model or pin a quantization, dropping a new `.gguf` +
`.Modelfile` on the VPS is the only step required.

### What needs to be on the VPS

A new script, `infra/setup-mirror.sh`, populates the mirror:

- `https://ai.dallinlayton.com/download/ollama-setup.exe` —
  mirrored from `https://ollama.com/download/OllamaSetup.exe`
  (~700 MB Squirrel installer; runs with `/S` for silent).
- `https://ai.dallinlayton.com/download/models/llama3.2-1b.gguf` —
  Q8_0 quantization to match Ollama's default for that tag
  (~1.3 GB; pulled from a HuggingFace mirror of
  `meta-llama/Llama-3.2-1B-Instruct`).
- `https://ai.dallinlayton.com/download/models/llama3.2-1b.Modelfile`
  — written inline by the script; the `FROM` line is a placeholder
  the agent rewrites to an absolute path before invoking
  `ollama create`.

Disk on VPS: ~2 GB additional. Bandwidth out: 2 GB per new
contributor's first install (one-time). Hetzner CPX21 has 80 GB disk
and 20 TB/mo bandwidth, so 10 contributors/mo costs 20 GB of egress
— a rounding error.

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.235.139
sudo bash /opt/gamerai/infra/setup-mirror.sh
```

Runs idempotently; re-run after `git pull` is harmless.

### Why install Ollama unelevated

Ollama's Windows installer is a Squirrel-based exe that installs into
`%LOCALAPPDATA%\Programs\Ollama` and does *not* require admin
elevation. Since our own installer is `PrivilegesRequired=lowest` and
the agent runs unelevated, this works out — no UAC prompt mid-install,
no elevation friction. The tradeoff: per-user install means each
Windows account on a shared machine needs its own Ollama. Fine for
single-user gamer rigs; will revisit if a real contributor reports
multi-account hosts.

### Model capability advertised on /register

`/register` now sends `capabilities: {"models": ["llama3.2:1b"]}` when
bootstrap succeeds (i.e., when `OLLAMA_URL` is set after bootstrap
returns). `/workers` already surfaces this; the only thing still
missing is *routing*: today every job goes into the global queue, so
a job targeting `llama3.1:8b` could still land on a contributor that
only has `llama3.2:1b`. That worker would call Ollama, get a 404
model-not-found, and fall through to the mock-fallback path —
correct behavior, but wasteful. Capability-aware routing is the
deferred Phase 4 piece.

### What's still NOT in this slice

- **Per-tier model bundles.** Today every contributor installs the
  same `llama3.2:1b`. A BRONZE 6GB GPU should probably still get
  the 1B; a PLATINUM 24GB GPU should get a 13B-class model
  automatically. Needs (a) coordinator-side advertisement of "what
  this tier should run" and (b) capability-aware routing. Out of
  scope until tier auto-promotion ships.
- **Disk-space pre-check.** A contributor with <5 GB free will fail
  mid-download. We log the failure but don't catch it upfront.
- **Resume on partial download.** A flaky network on a 1.3 GB pull
  starts over from scratch. httpx supports Range, but the failure
  rate of small-model downloads in practice is low enough this isn't
  worth the complexity yet.
- **Signed Ollama installer mirror.** We're rehosting an unsigned
  bag of bytes. If our SFTP key leaks, an attacker could swap in a
  malicious "OllamaSetup.exe" and we'd serve it. Same trust chain
  as the agent self-update; signed binaries are the eventual EV-cert
  fix (still Phase 4).
- **Background-mode bootstrap progress UX.** The bootstrap can take
  3–5 minutes the first time. In `--background` mode there's no
  console; the contributor sees nothing happen for several minutes
  unless they tail `%APPDATA%\GamerAI\logs\agent.log`. A
  status-shows-up-in-system-tray UI would help, but it's beyond
  this slice.

### Test scoreboard

- 118/118 tests pass (108 + 10 new in `tests/test_agent_bootstrap.py`:
  config defaults, user overrides, slug shape, disabled/non-Windows
  short-circuit, `/api/tags` parsing).
- Existing tests untouched; bootstrap is gated on `IS_WINDOWS` so the
  Linux dev/CI path is a no-op.
- Real-machine verification still pending the next CI rebuild + a
  manual run on the founder's Windows box.

### File layout changes

```
windows-agent/agent.py            <- bootstrap_inference() + helpers,
                                     Coordinator.register(capabilities=...),
                                     bootstrap_* fields on Config
windows-agent/config.json         <- "bootstrap" section
                                     (enabled, model, ollama_url,
                                      mirror_base_url)
infra/setup-mirror.sh             <- one-time VPS-side script to
                                     populate /download/{ollama-setup.exe,
                                     models/llama3.2-1b.gguf,
                                     models/llama3.2-1b.Modelfile}
tests/test_agent_bootstrap.py     <- new
```

---

## 2026-05-12 — Background self-update for the Windows agent

Without an update path, every installed agent gets stuck at the
version the recruit installed. Yesterday's first real-machine run
made this concrete: the agent on the founder's Windows box predates
the keep-awake fix and would never pick it up. Today's slice closes
that gap.

### How it works

```
CI runs (windows-latest)                       Installed agent (Windows)
─────────────────────                          ──────────────────────
write version.txt = "<sha> <iso>"              read bundled version.txt
  ↓                                              ↓
PyInstaller bundles version.txt                every 6h, GET
  → agent.exe                                  /download/version.txt
  ↓                                              ↓
ISCC.exe → installer.exe                       compare to bundled
  ↓                                              ↓ (mismatch)
SFTP all three to                              stream new agent.exe
  /var/www/downloads-chroot/uploads             → agent.exe.new
  ↓                                              ↓
Caddy serves                                   write update.bat
  /download/{agent.exe,                         ↓
            installer.exe,                     launch detached, exit
            version.txt}                        ↓
                                               update.bat: taskkill +
                                                 move + start --background
```

### Trust chain — what an attacker would need

To push malware to every installed agent, an attacker would need
one of:

1. The CI SFTP private key (stored in GitHub Actions secrets).
2. Write access to the `Hyrumdrums/GamerAI` repo's main branch
   (to inject a malicious commit + trigger the workflow).
3. MITM on the contributor's HTTPS connection to
   `ai.dallinlayton.com` (Let's Encrypt cert, HSTS implied by
   the agent's httpx default verification).

None of those are zero — particularly (2). Real defense is signed
binaries (EV cert ~$300/yr); deferred until the contributor count
makes it worth the cost.

### Why "aggressive restart" was the right default

The other path I considered was "only update while idle AND no job
claimed." That sounds nice — your in-flight job doesn't get
requeued — but it has a failure mode: if the contributor's network
is steadily fed jobs (utilization > 0), the agent could go weeks
without ever hitting "idle AND no claimed job," and the version
would diverge indefinitely. Aggressive restart costs at most a
single requeued job (the reaper requeues anything claimed but
incomplete after JOB_TIMEOUT_SECONDS, so no data loss). Cheaper.

The `should_exit()` callback that main_loop now honors gives us a
clean place to wire in the "wait for no-claimed-job" mode if a real
contributor ever complains.

### The bootstrap catch-22

The currently-installed agent on the founder's Windows box was built
BEFORE this commit. It has no version.txt, no updater thread, no
awareness of `/download/version.txt`. To get the self-update
behavior the *first* time, the contributor has to manually reinstall
once. After that initial reinstall every future fix is automatic.

Same constraint will apply when we recruit strangers — each
contributor's first install is manual, every subsequent update
is silent.

### What's still NOT in this slice

- **Signed binaries.** EV cert costs money; not worth it for 3
  friends. Documented as a Phase 4 cost item.
- **Rollback on bad update.** If a new exe fails to start, the
  installed agent is bricked until manual reinstall. Mitigations
  are doable (keep a .bak copy, watchdog that checks for a
  heartbeat within N minutes, etc.) but each adds complexity.
- **Update-aware idle scheduling.** The agent exits at the next
  main_loop iteration, between jobs. Doesn't wait for "no jobs
  for 5 minutes" or similar. Reaper requeues anything caught
  mid-flight.
- **Update notifications on the coordinator side.** No metric for
  "X agents have updated to version Y." Could be useful for
  monitoring rollout health; defer until we have more than 3
  agents.

### File layout changes

```
windows-agent/version.txt         <- generated by CI, gitignored
.gitignore                        <- adds windows-agent/{version,dist,build,Output,upload}
.github/workflows/windows-agent-build.yml
                                  <- new step writes version.txt;
                                     bundles via --add-data;
                                     uploads alongside the two exes
windows-agent/agent.py            <- current_version(),
                                     fetch_latest_version(),
                                     _apply_update(),
                                     updater_loop() daemon thread,
                                     should_exit() hook in main_loop
windows-agent/config.json         <- update.enabled (true),
                                     update.check_interval_hours (6)
```

CI build `ba0d9a6` is the first build to publish a non-empty
`/download/version.txt`. Once it lands, an installed agent built
from this commit or later will start picking up updates
automatically every ~6 hours.

---

## 2026-05-12 — Agent reliability: keep-awake, drain visibility, abandon path

Yesterday's first real-machine run surfaced two contributor-side
gaps: the agent went silent after Chrome Remote Desktop disconnect
(Windows modern-standby), and the "agent finishes current job
before going offline" behavior advertised in README_addendum was
real but completely invisible to the contributor (no log line, no
console output). Today's slice fixes both, and adds an opt-in
override path for the future paid-contributor case.

### What shipped

**1. Keep-awake (Windows `SetThreadExecutionState`).** When the
agent is launched with `--background` (the installer-autostart
path) and `power.keep_awake_while_online` is true (default), the
agent calls `SetThreadExecutionState(ES_CONTINUOUS |
ES_SYSTEM_REQUIRED)` at startup. The flag stays active for the
lifetime of the agent process; released on exit. Windows treats
this as "something needs the system" and skips the idle-timer →
sleep path. Foreground / `--once` runs never touch power state —
they're dev/test workflows.

Yesterday's argument against this was "real gaming PCs configure
sleep to never; SetThreadExecutionState is pushy and redundant for
them." Counter-argument that landed: gaming PCs are a minority of
the recruit pool, and even gamers lock screens / use Remote
Desktop. The opt-in framing (autostart toggle + config knob)
preserves "your machine, your rules": foreground users see no
behavior change; autostart users get the silent uptime promise
they thought they were signing up for.

**2. Drain-visibility logging.** `main_loop` now remembers the
last-processed `job_id` across iterations. The transition
"finished a job → user just became active" emits a distinct line:

```
user activity detected (user active (0s since last input)) —
last job 3b160373-... complete, agent offline
```

Plain idle-to-offline transitions (no job in flight) keep the
silent heartbeat-only path; only the drain case logs. The
rotating file at `%APPDATA%\GamerAI\logs\agent.log` now tells
the story the README addendum promised.

**3. Override-drain (`idle.override_drain`, default false).** A
new opt-in config knob for the "paid contributor who'd rather
forfeit earnings than make the user wait" case. When true, the
agent re-checks system idle between *claim* and
*inference-start*; if the user became active in that window,
the agent calls a new `POST /jobs/abandon` and bails out.
Coordinator requeues the job via the existing
`db.requeue_job` + Redis processing-hash unwind path the reaper
already used.

What this does *not* do today is interrupt mid-inference. Real
mid-inference cancel needs streaming inference + a cancellation
token, which is a larger surface (httpx streaming + signal
handling). Documented in the agent comment. Hooked up for the
small pre-inference window so the override knob is real even if
it's only catching a sub-second activity window for mock
inference.

### Coordinator changes

- **`POST /jobs/abandon`** in `coordinator/main.py`. Body:
  `{worker_id, job_id}`. Returns `{ok, requeued, reason?}`.
  Idempotent — a missing/unknown `job_id` is a no-op
  (`requeued: false`). Reuses the same Redis + SQLite unwind
  the reaper uses for timeout requeues, so a job comes back
  to the queue in exactly the state any other worker can
  pick up.
- Two new tests in `tests/test_coordinator_e2e.py`:
  `test_abandon_returns_job_to_queue` (happy path with full
  state-machine assertion) and `test_abandon_unknown_job_is_noop`.

### Why we shipped #3 even though paid contributors are months
away

Three reasons it was worth landing now rather than punting until
Phase 3b.ii:

- The coordinator endpoint is generally useful — same shape
  as the reaper-requeue path, no new concept, ~50 lines.
- The agent-side check between claim and inference is a single
  if-block; cheap to add now, free to leave dormant
  (`override_drain: false` is the default).
- It surfaces the design constraint clearly *before* we have a
  real paid contributor whose expectations need managing. When
  the time comes, "we already have an abandon API and an
  opt-in agent knob; we just need streaming inference to make
  it interrupt mid-job" is a much smaller conversation than
  "we need to design this from scratch."

### What's still NOT in this slice

- **Mid-inference cancel.** Today abandon only catches activity in
  the claim→inference-start window (<1s). The honest answer is
  "your override only saves time if your inference is slow to
  start, not if it's slow to finish." For the current network
  (sub-10s mocks, sub-30s small-model inference) this is fine; for
  paid-quality 13B-class inference where a single job can take
  many seconds, mid-inference cancel becomes meaningful and the
  architectural cost (streaming + cancellation tokens) becomes
  worth paying.
- **Windows service mode.** Still not in. Yesterday's argument
  stands — Session 0 GPU access is sketchy and shipping a service
  install adds elevation friction to the recruitment flow. The
  keep-awake feature above is the cheap intermediate fix. If
  future contributors actually complain that "I had to log in
  for the agent to start," we revisit and probably ship service
  mode as an optional advanced install path.
- **Foreground-mode keep-awake.** Deliberately off. Foreground
  is "I'm watching the agent" and the user's power preferences
  should rule. Only the autostart-as-background-contributor mode
  opts into keep-awake.

### Test scoreboard

- `coordinator/main.py` + `coordinator/db.py` unchanged surface,
  one new endpoint.
- 108/108 tests pass.
- CI rebuild of the Windows agent kicked off on commit `0d09e59`;
  fresh `agent.exe` + `GamerAI-Agent-Setup.exe` should land at
  `https://ai.dallinlayton.com/download/` within ~1m.

---

## 2026-05-12 — First real-machine onboarding run + sleep behavior

First end-to-end recruitment test on a real Windows host (not the
founder's earlier local dev test). Worked end-to-end. One real-world
quirk worth recording before we recruit a stranger.

### What ran

- Admin minted a contributor token for `hyrumdrums@gmail.com` via
  the CLI (`python -m coordinator.admin create-member --role
  contributor --email …`).
- Token pasted into the installed `agent.exe`'s first-run prompt
  on a Windows host reached over Chrome Remote Desktop. Persisted
  to `%APPDATA%\GamerAI\state.json`.
- Worker registered with the production coordinator with
  `worker_id=win-Home-PC-d4f76ae1`. Heartbeats every ~5s.
- Status flipped correctly between `idle` (no kbd/mouse for 60s,
  CPU < 30%) and `offline` (user touching the keyboard) — the
  gamer-friendly contract held without intervention.
- Admin submitted a `/generate` prompt from a separate Linux box.
  The first job was served by the in-VPS mock worker (`worker-fa…`)
  in 7.5s — its 2.5s polling interval beat the Windows agent's 5s
  poll plus 60s idle gate.
- Stopped the in-VPS mock worker (`docker stop gamerai-worker-1`)
  to force the next job onto the Windows machine. Job submitted.

### The quirk: Windows sleep ends the contributor's session

About 4 minutes after Chrome Remote Desktop disconnected from the
Windows host, the agent stopped heartbeating
(`hb_age=235s`). Likely cause: Windows modern-standby /
S0-low-power kicked in after the RDP session ended, suspending
user-session processes including `agent.exe`. Job sat in the queue
with `status: pending`, `queue_depth: 1`, `active_workers: 0`.

Implications for the recruitment pitch:

- **The model is fine when the contributor's machine is left
  awake.** Most gaming PCs are configured "never sleep" by their
  owners; for them the agent runs through the night and serves
  whenever idle. The whole "leave it on overnight" pitch in
  README_addendum already assumes this.
- **But for non-gamer Windows boxes with default power settings**,
  the contributor disappears from the network whenever they walk
  away. The agent has no power-management awareness today —
  no `SetThreadExecutionState(ES_SYSTEM_REQUIRED)`, no
  PowerCfg / wake-timer integration.

### Why we won't fix this in the agent today

Two reasons:

1. **It's a configuration question, not a code question, for the
   target user.** Real gaming PCs override the default sleep
   timer to never; that's the population we care about for the
   tier ladder + paid pool. Adding `SetThreadExecutionState` to
   the agent would *prevent* sleep on every host, which is
   pushy and breaks the "your machine, your rules" framing in
   `README_addendum.md`'s safety notes.

2. **Graceful pending-state is the right failure mode.** When a
   contributor's machine sleeps, jobs they would have served sit
   in the queue until another worker picks them up — or the
   reaper requeues them after `JOB_TIMEOUT_SECONDS` if they
   were claimed. No data loss, no error path, no spam to the
   contributor's logs. The cost is latency, which is exactly
   what we accept by saying "yes, latency-tolerant
   workloads."

What we *should* do is be honest about this in the README
addendum's "How it works" section, and add a one-line note to the
redemption page on Step 3 (something like "the agent only takes
work while your PC is awake — leave it running overnight to
contribute more"). Both are docs work, not code.

### Adjacent observation: long polling vs short polling

The in-VPS mock worker's 2.5s poll beat the Windows agent's 5s
poll consistently. That's not surprising — the VPS worker is in the
same docker network with negligible round-trip — but it suggests
that **when we have multiple contributors in different locations,
the one with the lowest end-to-end coordinator round-trip will
serve any given job**, all else equal. Could matter for the
geographic contributor recruiting motion (project_network_economics
memory). Not a fix today, just a note.

### Today's seven commits + this finding

```
db55ebc  feat(membership): per-member identity, invite flow, daily quota gate
e5c0858  test(web): smoke coverage for every web UI page
3b1ae07  fix(caddy): route /invite/<code> to the web UI
35d27b6  infra: serve /download/ from /var/www/downloads via Caddy
a9f86d4  ci: windows-agent build + SFTP-publish to ai.* downloads
37b6fc1  docs(devlog): record windows agent build + gotchas
7d61d68  feat(onboarding): redemption page → installer + first-run token prompt
6aedafb  docs(devlog): record onboarding loop closure
```

Plus this finding. Membership + invite + download + install +
first-run token + register + heartbeat + idle gating ALL worked
on a real Windows machine on the first try. Sleep behavior is
the only thing that broke flow, and only in the
remote-managed Windows case.

---

## 2026-05-12 — Onboarding loop closure (redemption page → installer)

Same date as the build-surface entry below. The previous slice put
`agent.exe` and `GamerAI-Agent-Setup.exe` at
`https://ai.dallinlayton.com/download/...`, but the redemption page
just showed Bob his bearer token and said "paste it into the client
config of your choice." For a non-developer recruit, that's exactly
where the onboarding silently died. This slice closes the loop.

### What the recruit sees now

1. Clicks the invite URL → public redemption page (HTML from
   `client/web.py`, routed by Caddy's `/invite/*` rule).
2. Clicks **Accept** → post-accept page with three numbered steps:

   - **Step 1: Save your bearer token.** The token is shown in a
     yellow box with a one-click **Copy** button (uses the
     `navigator.clipboard` API; falls back to text-selection on
     older browsers).
   - **Step 2: Install the Windows agent.** A prominent blue
     **Download installer** button links straight to
     `/download/GamerAI-Agent-Setup.exe`. A small note explains
     the SmartScreen "More info → Run anyway" dance (binary is
     unsigned for now; EV cert is a slice-4 cost).
   - **Step 3: Paste your token on first run.** Explains that
     `agent.exe` will prompt for the token the first time it
     launches.

   Power-user footer offers the standalone `/download/agent.exe`
   and direct API usage against `https://ai.dallinlayton.com` with
   `Authorization: Bearer <token>`.

3. Bob runs the installer, launches the agent, gets:

   ```
   GamerAI agent first-run setup
   -----------------------------
   Paste the bearer token from your invite redemption page.
   Looks like:  gai_<64 hex chars>

   token:
   ```

   He pastes (Ctrl+V), the agent validates the shape (must start
   with `gai_`), writes it to `%APPDATA%\GamerAI\state.json`, and
   then registers + starts polling against
   `https://ai.dallinlayton.com`.

4. On every subsequent launch the agent reads the token from
   `state.json` without prompting — including in `--background`
   mode after the user ticks the "run on Windows startup"
   installer option.

### Code changes

- **`client/web.py`** — `_REDEEM_DONE_PAGE` rewritten. New CSS for
  the copy button + download CTA. New `<script>` block hooks the
  Copy button to the clipboard API. Three numbered `<h2>` steps.
- **`windows-agent/config.json`** — `coordinator_url` default
  flipped from `http://localhost:8000` to `https://ai.dallinlayton.com`.
  Bundled into both `agent.exe` (via PyInstaller `--add-data`) and
  the installer (per `installer.iss`). Local devs running from
  source override via `--config` or by editing their working copy.
- **`windows-agent/agent.py`** — new `resolve_api_token()` function
  with the chain: env `API_TOKEN` → `config.json` → `state.json` →
  interactive prompt. Background mode without a token exits 2 with
  a clear message pointing at `state.json`. The prompt enforces
  the `gai_` prefix as a paste-accident guard.

### Test additions

- `tests/test_web_ui_smoke.py::test_invite_accept_page_links_to_installer`
  asserts the post-accept page contains both download URLs and the
  copy button. Without it a future refactor could silently strip
  the install hook and the onboarding path would only break when
  a real recruit hit it.

### Subtle things worth remembering

**1. The first-run prompt is the right home for the token, not
config.json.** Initially I considered an installer-side dialog
("paste your token now") to bake it into config.json at install
time, but Inno Setup's `[Code]` dialog support adds a Pascal-script
wart for one user-facing field. The agent's own first-run prompt is
free (Python `input()`), survives uninstalling and reinstalling the
binary (state.json lives in `%APPDATA%`), and decouples the
"install" event from the "have a token" event. Same pattern as
many CLI tools — first run is interactive, every subsequent run is
automatic.

**2. State is the right place to persist user secrets, not
config.** `config.json` ships with the binary and gets blown away
on reinstall. `state.json` lives in `%APPDATA%\GamerAI\` and
survives the binary being replaced. Token belongs there, alongside
`worker_id` and cumulative earnings.

**3. The build pipeline rebuilds on every push that touches
`windows-agent/**`.** That includes `config.json` and `agent.py`,
so the new defaults shipped on the same `git push` that fixed the
redemption page. Worth keeping the workflow's `paths:` filter
honest — if we ever move agent-related Python out of `windows-agent/`,
the build won't fire automatically.

### Bytes-on-disk on /download/ after this slice

```
$ curl -s https://ai.dallinlayton.com/download/BUILD.txt
GamerAI Windows agent — build 7d61d68 — 2026-05-12 01:38 UTC
Repo commit: 7d61d6822fc9b8bb9d3a8eb2b5d08e7b573238b4
```

agent.exe: 10.4 MB (unchanged shape), installer: 12.8 MB (slight
size bump from the new `coordinator_url` literal). Two PyInstaller
+ ISCC runs total today, both ~1m on `windows-latest`.

---

## 2026-05-12 — Windows agent build + public download surface

Goal: a non-developer recruit clicks an invite URL, gets their bearer
token, then downloads `GamerAI-Agent-Setup.exe` and clicks through.
Today's session built that download surface end-to-end and fixed two
prod bugs we hit along the way (Caddy didn't know about
`/invite/<code>`; the GitHub deploy key wasn't registered after the
prior VPS was retired).

### What's now live

- **`https://ai.dallinlayton.com/download/agent.exe`** — 10.4 MB
  standalone PyInstaller `--onefile` build of `windows-agent/agent.py`,
  with `config.json` bundled.
- **`https://ai.dallinlayton.com/download/GamerAI-Agent-Setup.exe`** —
  12.2 MB Inno Setup installer (Start Menu shortcuts, optional
  run-on-startup tickbox, uninstaller).
- **`https://ai.dallinlayton.com/download/BUILD.txt`** — manifest with
  the source commit SHA + UTC build timestamp.
- **`/download/` index** — Caddy `file_server browse` so a recruit
  can land and see what's available without guessing filenames.

### Pipeline shape

```
git push origin main (touches windows-agent/**)
  ↓
.github/workflows/windows-agent-build.yml on windows-latest
  ↓
pip install -r windows-agent/requirements.txt
  ↓
pyinstaller --onefile --name agent --add-data "config.json;." agent.py
  → dist\agent.exe (~10 MB)
  ↓
choco install innosetup -y
ISCC.exe installer.iss
  → Output\GamerAI-Agent-Setup.exe (~12 MB)
  ↓
sftp gamerai-uploads@$VPS_HOST:uploads/  (chroot-only user; can ONLY
                                          write into that one dir)
  ↓
Caddy file_server serves /var/www/downloads/* over TLS
```

End-to-end build is ~1m 13s on a `windows-latest` runner.

### Caddy routing for the public surface

`infra/Caddyfile` now has three handlers under `{$DOMAIN}`:

```
handle_path /download/*       → file_server (Caddy serves directly)
handle /invite/*              → reverse_proxy client:8080  (web UI HTML)
handle (everything else)      → reverse_proxy coordinator:8000  (API)
```

The `/download/*` block uses `handle_path` (strips the prefix) so
`/download/agent.exe` resolves to `/srv/downloads/agent.exe` inside the
Caddy container. `/srv/downloads` is bind-mounted from
`/var/www/downloads` on the host (see `infra/docker-compose.prod.yml`),
read-only inside the container. Caddy never writes.

### Restricted SFTP-only user (`gamerai-uploads`)

Per-host state on the VPS (not in the repo because it's not
declarative):

```bash
# System user, no shell, no password.
useradd -r -M -s /usr/sbin/nologin gamerai-uploads

# Chroot directory MUST be root-owned with no group/other write,
# otherwise sshd refuses to chroot into it.
mkdir -p /var/www/downloads-chroot/uploads
chown root:root /var/www/downloads-chroot
chmod 755 /var/www/downloads-chroot
chown gamerai-uploads:gamerai-uploads /var/www/downloads-chroot/uploads

# Bind-mount the writable subdir into Caddy's serving path.
# Persisted across reboot via /etc/fstab.
echo "/var/www/downloads-chroot/uploads /var/www/downloads none bind 0 0" \
  >> /etc/fstab
mount /var/www/downloads

# Authorized key OUTSIDE the chroot — sshd reads it before chrooting.
mkdir -p /etc/ssh/auth
echo "ssh-ed25519 AAAA... ci-upload" > /etc/ssh/auth/gamerai-uploads

# sshd_config block (idempotent, appended once):
cat >> /etc/ssh/sshd_config <<CFG
Match User gamerai-uploads
    ChrootDirectory /var/www/downloads-chroot
    ForceCommand internal-sftp
    AllowTcpForwarding no
    X11Forwarding no
    PasswordAuthentication no
    AuthorizedKeysFile /etc/ssh/auth/%u
CFG
systemctl reload ssh
```

Blast radius if the CI key leaks: the attacker can read/write/delete
files inside `/var/www/downloads-chroot/uploads`. They cannot run
shell commands (`ForceCommand internal-sftp`), cannot escape the dir
(`ChrootDirectory`), cannot forward ports
(`AllowTcpForwarding no`), and cannot reach any other path. The
worst case is "they put a malicious .exe at our download URL" —
serious, but small surface and easy to detect/rotate.

### Repo secrets (GitHub Actions)

```
VPS_SSH_KEY      private ed25519 key — gamerai-uploads@VPS
VPS_HOST         5.161.235.139
VPS_KNOWN_HOSTS  output of `ssh-keyscan -H 5.161.235.139`
```

Locally, the CI private key is at `~/.ssh/gamerai-ci-upload`.

### Gotchas worth remembering

**1. `caddy reload` reported success but did not apply the new route.**
After committing the `/invite/<code>` → `client:8080` fix and running
`docker exec gamerai-caddy caddy reload --config /etc/caddy/Caddyfile
--adapter caddyfile`, the response continued to be the coordinator's
JSON 401 instead of the redemption HTML. The reload command emitted:

```
"adapted config to JSON"
"Caddyfile input is not formatted; run 'caddy fmt --overwrite' to fix"
```

…and *no* "config loaded" line. A full `docker restart gamerai-caddy`
applied it cleanly. Lesson: don't trust the reload's exit code alone —
verify with a request afterward, and fall back to a container restart
if the route isn't taking.

**2. GitHub deploy key for the private repo was missing on the new VPS.**
Bootstrap failed at `git clone git@github.com:...` with
`Permission denied (publickey)`. `gh api repos/Hyrumdrums/GamerAI/keys`
returned `[]`. The previous VPS used this same `gamerai-github-deploy`
key but GitHub or the user must have removed it when the prior server
was destroyed (or it was never on this repo). Fix:

```bash
gh api repos/Hyrumdrums/GamerAI/keys -X POST \
  -f title="gamerai-github-deploy" \
  -f key="$(cat ~/.ssh/gamerai-github-deploy.pub)" \
  -F read_only=true
```

Add to the rebuild-from-scratch runbook: **verify the deploy key is
registered before running bootstrap**, since `bootstrap.sh` will hard-fail
at the clone step otherwise. The bootstrap output already references
this key path; the missing piece is the GitHub-side registration check.

**3. Pushing `.github/workflows/*.yml` over HTTPS via the gh CLI's
token requires the `workflow` OAuth scope.** First push of the new
workflow file was rejected with:

```
refusing to allow an OAuth App to create or update workflow
.github/workflows/windows-agent-build.yml without `workflow` scope
```

Fix: switched the remote URL from HTTPS to SSH
(`git remote set-url origin git@github.com:...`), since `gh auth
status` had already configured SSH for git operations. SSH push went
through immediately. Alternative: `gh auth refresh -s workflow`.

**4. Inno Setup isn't preinstalled on `windows-latest` runners.**
Chocolatey is, so `choco install innosetup --no-progress -y` is the
one-liner that gets `ISCC.exe` on PATH (well, at
`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`).

**5. The `/admin/members` and `/admin/invites` HTML pages in
`client/web.py` are not on the public domain.** The current Caddyfile
only routes `/invite/*` to the web UI; everything else goes to the
coordinator. So the admin pages still require an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 root@VPS` → `http://localhost:8080/admin/...`).
That's intentional for now (no basic_auth in place yet) but means
"open the admin dashboard in your browser" is not yet a one-click
operation.

### What's not in this slice (yet)

- **Signed binaries.** PyInstaller output is unsigned. Windows
  SmartScreen will show "Unrecognized app" the first time. Real code
  signing needs an EV certificate (~$300/yr); not worth it for the
  3-friend recruitment phase. A note on the redemption page telling
  Bob to "click More info → Run anyway" is the slice-3 polish.
- **Redemption page → download link.** The invite-redemption HTML
  currently shows Bob his token but doesn't link to the installer.
  Hooking the two together so the page says "Step 2: download
  `GamerAI-Agent-Setup.exe`, paste this token during install" is the
  natural next polish.
- **Auto-update for an installed agent.** A future fix-ship still
  requires the user to re-download manually. Squirrel/NSIS
  auto-updater is on the project-gaps list.

### Bytes-on-disk + commits today

```
db55ebc  feat(membership): per-member identity, invite flow, daily quota gate
e5c0858  test(web): smoke coverage for every web UI page
3b1ae07  fix(caddy): route /invite/<code> to the web UI
35d27b6  infra: serve /download/ from /var/www/downloads via Caddy
a9f86d4  ci: windows-agent build + SFTP-publish to ai.* downloads
```

Five commits, 3 production fixes, 2 new public surfaces (redemption
page + download index), 105 tests still passing, ~$0.06 of Hetzner
+ ~1.3 min of GHA `windows-latest` time spent.

---

## 2026-05-11 — Invite flow + quota enforcement (slice 2 of 2)

Slice 2 lands the invite UX and the daily-quota gate. Contributors can
now invite outsiders without bothering the admin. Invitees redeem a
copy-paste URL, get a bearer token, and start submitting prompts —
gated by their daily quota.

### What changed

- **`invites` table** in `coordinator/db.py`: invite_id, code (unique),
  contributor_member_id, invitee_email, daily_quota_tokens, expires_at,
  accepted_at, accepted_by_member_id, revoked_at, notes, created_at.
  Indexed on `code` and `contributor_member_id`.
- **`accept_invite_atomic`** in `coordinator/db.py`: single
  `BEGIN IMMEDIATE` transaction that validates the invite is open,
  inserts the new member row, and stamps `accepted_at /
  accepted_by_member_id` on the invite. Concurrent accepts of the
  same code cannot both succeed.
- **Invite endpoints in `coordinator/main.py`:**
  - `POST /invites` — contributor or admin creates. Returns code.
  - `GET /invites?all=true` — listing. Admin gets the full table;
    contributors get their own.
  - `GET /invites/<code>` — **public.** Returns inviter email +
    cap so the redemption page can render before Bob has a token.
  - `POST /invites/<code>/accept` — **public.** Mints the invitee
    member, returns the bearer token exactly once.
  - `POST /invites/<code>/revoke` — admin-only, unredeemed invites
    only.
- **Method-aware public-path matching** in the auth middleware
  (`_is_public(method, path)`). Without it, `POST
  /invites/<code>/revoke` was also being treated as public, which
  defeated the admin gate. Lesson: when path prefixes overlap between
  public and authenticated endpoints, the matcher needs path-shape +
  method specificity, not just a `startswith`.
- **Daily quota enforcement on `/generate`.** When the authenticated
  member's `daily_quota_tokens` is non-null, the coordinator checks
  today's `member_usage.tokens_out` against the cap and returns 429
  if at/over. Pre-estimating completion size is out of scope —
  the first prompt that crosses the cap can overshoot slightly. NULL
  cap = unlimited (admin, tier-unlimited contributor).
- **`/admin/members`** — admin-only roster endpoint. 403 for
  non-admins. Returns no raw tokens (those aren't stored).
- **CLI subcommands in `coordinator/admin.py`:** `create-invite`,
  `list-invites`, `revoke-invite`. `create-invite` prints the
  redemption URL using `PUBLIC_BASE_URL` (defaults to
  `http://localhost:8080`; override at deploy time).
- **Public redemption page in `client/web.py`:** `GET /invite/<code>`
  renders an HTML landing page; `POST /invite/<code>` submits the
  accept and shows the one-shot token. Uses a new `_public_client()`
  helper that strips the admin auth headers — the invite code is the
  credential, not the admin token.
- **Admin web pages in `client/web.py`:** `/admin/members` and
  `/admin/invites`. Plain HTML tables; relies on `client/web.py`
  inheriting `API_TOKEN` (so it talks to the coordinator as admin).
  Protected today by the localhost bind + SSH tunnel; layer Caddy
  basic_auth before recruiting non-friends.

### Tests

86/86 pass (`python -m unittest discover -s tests`). New cases:
- Invite create / public details / accept / one-shot / revoke / expiry.
- Invitee role cannot create invites (403).
- `/admin/members` requires admin role.
- Quota under cap → 200, over cap → 429, admin unbounded.

### What's still NOT in slice 2

- **Worker → member link.** Contributor earnings live on `worker_id`;
  consolidating per person is slice 3.
- **Tier auto-promotion engine.** Everyone stays BRONZE unless bumped
  by hand. Daily cron driven by uptime + claim rate is the next
  build.
- **Per-member auth on the web UI itself.** `client/web.py` still
  talks to the coordinator as admin for every viewer. Alice would
  see admin data if she visited. Real session/login flow against
  member tokens is its own piece of work.
- **SMTP-delivered invites.** Copy-paste URL is the slice-2 cut.
  Plug in Resend / Postmark when first usability complaint lands.
- **Caddy basic_auth on the web UI.** Required before the web UI is
  reachable beyond the SSH tunnel.

### How to run the new flow end-to-end (live VPS)

```bash
# 1. Create a contributor.
docker exec -it gamerai-coordinator python -m coordinator.admin \\
    create-member --role contributor --email alice@example.com
# → token=gai_<...>

# 2. Alice creates an invite for Bob.
docker exec -it gamerai-coordinator python -m coordinator.admin \\
    create-invite --contributor-token gai_<alice> \\
    --daily-quota-tokens 5000 --email bob@example.com
# → code=inv_<...>
# → redemption_url=http://localhost:8080/invite/inv_<...>
#   (set PUBLIC_BASE_URL=https://ai.dallinlayton.com in the coordinator
#    env to print the public URL instead)

# 3. Alice texts the URL to Bob; he clicks; redemption page mints his token.
# 4. Bob's first /generate call uses his bearer; quota enforced from there.
```

---

## 2026-05-11 — Member-identity layer (slice 1 of 2)

The 50 tok/s Windows-agent test proved the **agent**, not the **network**.
Every prompt so far has been the founder's own, served by the founder's
GPU through the founder's coordinator. The next step that makes that
statement false is "a second person's machine serves a stranger's
prompt." Slice 1 is the smallest cohesive change toward that —
per-member identity replacing the single shared `API_TOKEN`.

### What changed

- **New `members` table** in `coordinator/db.py`. Columns: `member_id`,
  `email`, `role` (`admin`/`contributor`/`invitee`), `parent_member_id`
  (the invite chain), `token_hash` (sha256), `tier` (default `BRONZE`),
  `daily_quota_tokens` (nullable = unlimited), `revoked_at`,
  `created_at`, `last_active_at`. Indexed on `token_hash` and
  `parent_member_id`.
- **`member_usage` per-day rollup** table. Updated on `/jobs/complete`
  when the original submitter is known. Sets up slice-2 quota
  enforcement without doing it yet.
- **`jobs.submitted_by_member_id`** column added via additive
  `ALTER TABLE` migration in `DB._migrate()`. Backfill is NULL —
  pre-membership jobs simply don't have a submitter attribution.
- **`coordinator/member_auth.py`** — server-side bearer lookup.
  `gai_<64 hex>` token format, sha256 hashing, constant-time compare,
  Member dataclass.
- **Middleware swap in `coordinator/main.py`** — replaces the single
  binary `check_authorization` with per-token member lookup. Attaches
  `request.state.member` so downstream handlers know who's calling.
- **Admin-seed-from-API_TOKEN.** On startup (or via direct
  `ensure_admin_seed()` call), if `API_TOKEN` is set, a member with
  role `admin` and tier `PLATINUM` is auto-created with that token's
  hash. Every existing client that already sends
  `Authorization: Bearer $API_TOKEN` is now logged in as that admin
  member. **Zero client-side migration.**
- **`coordinator/admin.py` CLI** — `create-member`, `list-members`,
  `revoke`. The raw token is printed exactly once at create time and
  never stored.
- **`GET /me`** returns the caller's identity + quota + today's usage.
  Cheap to add now, unblocks the slice-2 admin web UI.
- **`/generate` records submitter** on the job row. `/jobs/complete`
  attributes consumption to the member's daily usage.
- **`/result/{job_id}`** now surfaces `submitted_by_member_id` so a
  caller can tell whose job it was.

### Tests

73/73 pass (`python -m unittest discover -s tests`). New:
`tests/test_members.py` (data layer, 18 cases) and
`tests/test_member_auth_e2e.py` (HTTP layer with auth on, 12 cases).

### Bootstrap gotcha (test infra)

The existing `tests/test_coordinator_e2e.py` clears
`shared.X`/`coordinator.X` submodule entries from `sys.modules` to pick
up new env values on re-import. That's not enough: the **package
modules themselves** (`coordinator`, `shared`) retain attribute
references to first-loaded submodules, so a later `from coordinator
import main` returns the stale module — auth-off — and every member
test silently degraded to no-auth. Fixed in
`tests/test_member_auth_e2e.py` by clearing the package keys too:

```python
for _mod in list(sys.modules):
    if _mod.split(".", 1)[0] in ("shared", "coordinator"):
        del sys.modules[_mod]
```

Symptom that pointed at this: `/me` returned `{"auth_disabled": true}`
under `python -m unittest discover` but `{"role": "admin", ...}` when
the file ran in isolation. Order-dependent → module-state leakage.

### Backwards compatibility

- Auth-off (no `API_TOKEN` env) preserves all current behavior. No
  member rows are created; `submitted_by_member_id` stays NULL.
- Auth-on with the existing `API_TOKEN`: clients that already send
  `Authorization: Bearer $API_TOKEN` keep working — they're the admin
  member. No worker/agent config change required.

### Out of slice 1

Slice 2 (next, ~2–3 days):

- `invites` table.
- `POST /invites` (contributor creates), `POST /invites/accept/<code>`
  (invitee redeems → mints their own member token).
- Copy-paste invite URL (no SMTP — same as Tailscale/Discord first cut).
- Quota enforcement on `/generate` (reject when daily usage exceeds the
  invitee's cap).
- Minimal admin web UI in `client/` — list members, revoke, see invite
  chains.

Out of slice 1 and 2 both: tier auto-promotion engine, paid-pool /
Stripe rails, SMTP email delivery.

---

## 2026-05-11 — Business plan: community-powered, tier-based, layered paid

Second strategy pivot in three days. The 2026-05-09 entry reframed the
*tools* (AI toolbox, not chat-only). This one reframes the *economics*:
contribute-to-use with tier-based access for contributors and their
invitees, optional paid customer layer in Phase 3b.ii to fund the
network.

### Trigger

The Windows-worker throughput test landed at ~50 tok/s sustained on
llama-3.1:8b — a ~7.7× uplift over the CPU-only VPS baseline (6.5 tok/s
on llama-3.2:1b). Great capability demo. But the marketplace math
implied by yesterday's $5/1M-tokens pricing gave a contributor ~$1.30/day
of theoretical earnings — well below the marginal power cost of a
saturated gaming GPU.

The user pushed back: the payoff isn't a USD revenue split — **it's a
community AI service powered by their friends and neighbors**. The
revenue dance is the wrong axis to optimize against.

### Three-layer model (canonical)

1. **Foundation: contribute compute, earn tiered access (MVP).** Free
   contributor tiers (BRONZE → PLATINUM) gated by uptime + capability
   + claimed-jobs-per-hour. Tier sets quota and invite slots. Earning
   higher tiers is the entire engagement loop — no money required to
   participate.

2. **Optional paid customer layer (Phase 3b.ii, not MVP launch).** Three
   segments: CASUAL flat-fee households, DEVELOPER per-token API,
   ENTERPRISE volume contracts. Paid jobs land in a separate priority
   queue served only by opt-in GOLD+ contributors. Contributor-tier free
   access is *never* degraded by paid demand.

3. **Bonus payouts (Phase 3b.ii).** 80% of paid revenue flows to the
   contributor who served the paid job. 20% to the platform for
   coordinator infra + future development. The platform never extracts
   from contributor activity — only from paid activity, capped.

### Membership requirement — anti-freeloading

Contributing to the **shared network pool** is non-negotiable. The
agent serves the global queue anonymously, not just the contributor's
own invitees. Without this rule, a closed friend group could run the
agent only for their own circle and use the coordinator as a free
fancy LAN service. Modeled on BitTorrent ratios, Tor relays, mesh
Wi-Fi, Folding@home.

Tier maintenance requires both uptime AND claimed-jobs-per-hour, so a
fork that idles online without claiming jobs falls down the ladder.

**Architectural implication: zero.** Workers already `BLPOP` from a
global queue. Membership rule is a policy layered on top.

### Alice → Bob (invitee mechanic)

Contributors (Alice) invite non-contributors (Bob) by email. Alice
sets Bob's cap (% of her remaining quota or absolute token count) and
can adjust/revoke anytime via the host admin UI. Bob's prompts go
into the shared queue and are served by whichever contributor's
machine is idle — not specifically Alice's GPU.

Side effect: GOLD/PLATINUM contributors get more invite headroom,
which drives uptime competition organically. Status badge + invite
slots are the carrot, not USD.

### Power draw vs. uptime

Important for the contributor pitch:

| State | GPU draw (marginal) |
|---|---:|
| Online, no jobs (cold) | ~0 W |
| Online, model warm (Ollama keep-alive) | ~30 W |
| Active inference | ~250–400 W |

Power scales with demand, not uptime. "Leave it on overnight" costs
near-zero on a quiet network. Bursts of real power happen when there
is real demand, which is precisely when GOLD+ contributors are earning
paid-pool bonuses. Power bill and bonus are correlated, not decoupled.

The Ollama keep-alive window is a tunable knob — long for paid-heavy
workers (lower latency), short for casual contributors (lower idle
draw).

### Sustainability target

The founder's stated constraint: "I'm not made of money and can't
self-host the coordinator forever." Concrete answer:

- Coordinator infra: ~€8/mo today, ~$50/mo at 1k users, ~$200/mo at 10k.
- At $1.50/1M tokens to DEVELOPER paid customers (priced between Haiku
  and self-hosted), $50/mo break-even = 33M tokens/mo paid usage.
- At 50 tok/s saturated, that's ~6 hours/day of one PLATINUM
  contributor opted into the paid pool.
- **Two paying developer customers + one PLATINUM contributor covers
  the coordinator bill indefinitely.**

A year-one milestone, not a unicorn target.

### Privacy framing — corrected

Earlier drafts of this reframe said "your data stays in your trust
circle." That overstates it under the membership rule: prompts traverse
the contributor network, served by random members' GPUs (not
necessarily your inviter's machine). Honest version: "prompts stay in
the contributor network, not a hyperscaler — no training-on-prompts,
no surveillance harvesting." For sensitive use cases, Phase 5
**client-side embedding** is the answer — and now load-bearing for
enterprise paid customers, not optional garnish.

### What changed in the docs

- README § 1 — "marketplace" replaced with "community-powered AI suite";
  added membership-rule sentence + three-actor model
  (Contributors / Invitees / Paid customers).
- README § 5 (was "Business model") — renamed to "Economics," replaced
  with three-layer breakdown (tier ladder + paid tiers + 80/20 bonus
  split + sustainability math).
- README § 6 — "Worker value proposition" → "Contributor value
  proposition." Replaced "passive income" with status, access, invite
  slots, opt-in paid-pool bonuses. Added power-draw table.
- README § 7 — split into two audiences (contributors+invitees vs.
  paid customers). Added the honest privacy framing note.
- README § 14 Phase 3b — restructured into 3b.i (membership/tiers),
  3b.ii (paid layer), 3b.iii (trust & verification).
- README § 14 Phase 5 — added the community-network privacy context.
- `business.md` — new "Core mechanic — contribute-to-use" section, new
  3-loop "How it works" diagram, full Business Model rewrite, Target
  Customers split, Roadmap matched to README Phase 3 restructure.
- `docs/project-gaps.md` — "No customer identity" recast as "No
  membership identity / tier accounting"; "No payout rails" scope
  reframed to paid-revenue→bonus flow; "No pricing tier" expanded to
  span both contributor and paid sides; "No customer signup flow"
  replaced with "No invite / membership flow"; legal entry expanded
  to community ToS + contributor agreement + paid commercial ToS;
  30-day priority list reordered (membership/tiers/invites jump,
  Stripe/marketing/SDK fall).
- `windows-agent/README_addendum.md` — "How earnings work" rewritten
  to two parallel ledgers (contribution + bonus); safety-notes section
  on auth recast as the membership gate; power-draw table added to
  "How it works."

### Open implementation questions

- Exact thresholds for tier promotion/demotion — what hours/day, what
  jobs/hour ratio. Will need calibration once we have multiple real
  contributors.
- Ollama keep-alive policy per tier — should PLATINUM machines have
  longer keep-alives (lower latency, higher idle draw) than BRONZE?
- Contributor opt-in UX for the paid pool — a toggle in the agent? A
  daily/monthly opt-in window? Should there be a "minimum payout
  amount" the contributor can configure?
- Whether the coordinator should expose a leaderboard / public tier
  rankings, or keep tiers private.

### Late-day refinements (same date)

Subsequent discussion landed several additional model details, all now
in README §5 / §14 and business.md:

- **Tier promotion is instant** based on hardware + availability
  declaration; **paid-pool eligibility** requires 1-week sustained
  uptime + minimum claim rate. Decoupled to keep status frictionless
  while gating earnings on real reliability.
- **BATCH paid tier** added between CASUAL and DEVELOPER (~$0.75/1M
  tokens, <24h latency). The supply-soak lever — fills off-peak
  capacity at half price; modeled on AWS Spot. Most enterprise AI
  workloads (embeddings, classification, doc analysis) are
  batch-friendly.
- **Realistic earnings by GPU class** documented in §5: basic 3060
  nets ~$24/mo saturated, 4070 ~$58/mo, 4090 ~$87/mo (US-median
  electricity, after 80% worker share). Margin per million tokens
  holds 60–90% even in expensive electricity, but idle overhead bites
  basic GPUs — demand-driven uptime signals are load-bearing for
  3060-class contributors, not optional.
- **Time-of-day anti-correlation** identified as the #1 unsolved
  network constraint (paid demand peaks 9–6 weekdays, supply peaks
  overnight). Three structural fixes documented: BATCH tier,
  geographic contributor recruiting, eventual work-machine tier.
- **Utilization-driven signal loop** with concrete thresholds
  (50/70/85/90%) documented in §14 Phase 3b.ii. Two-direction
  acquisition: low util → paid-customer marketing (HN, dev forums,
  comparison pages); high util → geo-targeted contributor recruiting.
- **Notification UX** principle: tier framing first, earnings second.
  "Usage is growing — consider adjusting your uptime to reach the
  next tier" — earnings mention gated to paid-pool-eligible
  contributors only. No push notifications, no casino-style urgency.
- **Honest pre-onboarding messaging** about earnings: "PLATINUM
  qualifies you to earn; whether you do depends on demand." Visible
  network-demand stats in the agent dashboard to prevent
  disappointed-PLATINUM churn.

---

## 2026-05-09 — Strategy refresh: AI toolbox, not chat

Reframing the product from "distributed chat API" to "distributed AI toolbox"
of independent, retryable, latency-tolerant jobs. The architecture we already
built (job-based, queued, capability-aware on the data layer) supports this
without redesign — it just needs schema extensions and a second worker type.

### What changed in the docs

- README § 1 — toolbox framing + "Tools supported" table (chat MVP; image +
  search next; docs/code expansion; music/voice later; video and frontier
  training out of scope).
- README § 14 — old Phase 3 ("marketplace + dynamic pricing") split into
  **3a (multi-tool foundations)** and **3b (marketplace dynamics)**. 3a is
  now the next-up phase after the Windows-worker test.
- README § 10 — API table notes the planned `job_type` field on `/generate`
  and per-tool queue routing.
- `business.md` — added a "Supported tools" section, updated roadmap to
  reflect public deploy + toolbox.
- `docs/project-gaps.md` — capability-aware routing promoted from Phase 4
  to Phase 3a.

### Schema sketch (not yet implemented)

```
GenerateRequest:
  job_type: "chat" | "image" | "search"  (default "chat" for back-compat)
  params:   discriminated union of ChatParams | ImageParams | SearchParams

WorkerCapabilities:
  + tools: List[ToolType]  (e.g. ["chat", "image"])

Redis:
  job_queue:chat, job_queue:image, job_queue:search   (was: job_queue)

DB jobs table:
  + job_type TEXT NOT NULL DEFAULT 'chat'
  + result_blob TEXT   (base64/URL for non-text outputs)
```

Worker side: `BLPOP` over `[job_queue:t for t in capabilities.tools]`. No
new component, no broker rewrite — implicit routing via queue subscription.

Search runs *inside the coordinator* (no GPU lift); it fetches results, then
dispatches an internal chat job with augmented context. Image generation is
a real new worker mode (SDXL-class, ~8–12 GB VRAM).

### Sequence (not commitments — pending decision)

1. Real Windows worker test on chat (the milestone we were already heading
   toward — needed first, regardless of toolbox plan).
2. Phase 3a: `job_type` + capability routing schema, behind back-compat
   shim. ~3 days.
3. Web-search tool (centralized, no new worker type). ~1 day.
4. Image-gen worker mode + SDXL backend on Windows agent. ~1 week.
5. Unified toolbox UI (slash-commands or tabs).

### Open questions

- Earnings ledger needs per-tool rates (per-token doesn't apply to images).
  Cleanest schema: rename `total_tokens` → `total_units` and add a `unit`
  column. Defer until image gen actually ships.
- Quality variance across heterogeneous workers (1B vs 13B chat) is a real
  UX risk under the "abstract model complexity" principle. May need to
  expose a tier hint (Fast / Better / Best) instead of pretending the
  network is uniform.

---

## 2026-05-08 — Real inference on the VPS

VPS is no longer mock-mode. Currently serving `llama3.2:1b` from the in-VPS
worker over public TLS at `https://ai.dallinlayton.com/`.

### What changed on the box

```bash
# 1. Brought up the previously profile-gated ollama service
docker compose --env-file .env.prod \
  -f docker-compose.yml -f infra/docker-compose.prod.yml \
  --profile local-inference up -d ollama

# 2. Pulled the model (1.3 GB)
docker exec gamerai-ollama ollama pull llama3.2:1b

# 3. Flipped /opt/gamerai/.env.prod
MOCK_INFERENCE=false   # was true

# 4. Recreated worker so it picks up the new env
docker compose --env-file .env.prod \
  -f docker-compose.yml -f infra/docker-compose.prod.yml \
  --profile local-inference up -d worker
```

`infra/deploy.sh` doesn't include `--profile local-inference`. If you re-deploy
via the script, ollama won't come back up automatically. Either re-run the
profile-aware command above, or extend `deploy.sh` to detect when MOCK_INFERENCE
is false and pass the profile.

### Performance baseline

| Model | Hardware | Latency (38-prompt → 65-completion) | Throughput |
|---|---|---|---|
| `llama3.2:1b` | CPX21, 3 vCPU AMD, no GPU | ~10 s | ~6.5 tok/s |

Adequate for pipeline validation. Way too slow for customer-facing serving;
real inference is supposed to come from gamer machines.

### Resource state with model loaded

```
RAM total:    3.7 GB
Used:         1.9 GB  (ollama 1.94 GB / 3.7 GB — model weights resident)
Available:    1.5 GB
Swap:         none
```

Headroom is fine for the current workload. **Do not pull a 3B or 7B model on
this box** — it will OOM. Larger models go on gamer machines, not the VPS.

### Followups worth doing

- Decide whether the VPS keeps running real inference, or reverts to mock once
  external workers are connecting. Argument for reverting: VPS isn't supposed
  to be a worker, and the 1.9 GB of resident weight competes with future
  coordinator load. Argument for keeping: a no-workers-online state still has
  *something* to answer prompts.
- Teach `infra/deploy.sh` to honor a flag (or read `.env.prod`) so re-deploys
  don't silently lose the ollama service.
- Add a swap file (~2 GB) so an OOM doesn't kill the coordinator.

---

## 2026-05-07 — First public deploy

**Live:** https://ai.dallinlayton.com (Let's Encrypt cert, auto-renewed by Caddy)

### Infrastructure

| Thing | Value |
|---|---|
| Provider | Hetzner Cloud |
| Server | `gamerai-mvp` — CPX21 (3 vCPU AMD, 4 GB RAM, 80 GB SSD) |
| Region | Ashburn, VA (`ash`) |
| Public IP | `5.161.253.156` |
| Cost | ~€8.21/mo |
| OS | Ubuntu 22.04 |
| Firewall | Hetzner Cloud firewall `gamerai-public` (22/80/443 in) + ufw on box |
| Domain | `ai.dallinlayton.com` (Namecheap A record → public IP, TTL 300) |
| TLS | Let's Encrypt via Caddy 2 |

### Access

| Use | Key | Path |
|---|---|---|
| SSH to VPS | `id_ed25519_gamerai` | `~/.ssh/id_ed25519_gamerai` (local), `~/.ssh/authorized_keys` (VPS) |
| GitHub deploy key (read-only) | `gamerai-github-deploy` | local + `/root/.ssh/github_deploy` on VPS |
| Hetzner API context | local CLI | `~/.config/hcloud/cli.toml`, context `gamerai` |
| API_TOKEN (bearer auth) | random 64-hex | `/opt/gamerai/.env.prod` on VPS only — never committed |

### Stack on the box

`/opt/gamerai/` is the canonical install. Bootstrap clones to there.

```
gamerai-redis         redis:7-alpine                   127.0.0.1:6379  internal
gamerai-coordinator   gamerai-coordinator:latest       127.0.0.1:8000  internal (proxied via Caddy)
gamerai-worker-1      gamerai-worker:latest            (no ports)      MOCK_INFERENCE=true
gamerai-client        gamerai-client:latest            127.0.0.1:8080  internal (web UI, SSH-tunnel to view)
gamerai-caddy         caddy:2-alpine                   0.0.0.0:80,443  public
```

Real inference comes from external gamer machines connecting in, not the VPS.
The in-VPS worker is mock-mode so the system is alive before any gamers join.

### Decisions

- **Private repo + deploy key, not public.** Bootstrap was originally designed
  to `curl raw.githubusercontent...` anonymously; that returned 404 because
  the repo is private. Chose deploy key over going public — gives us the
  option to put real secrets / keys in the repo later without flag-day
  retrofitting.
- **Hetzner over DO/Lightsail.** ~½ the price for the same RAM/CPU, 20 TB
  bandwidth included, no managed services we need yet.
- **No IaC for now.** Manual VPS create (or `infra/hcloud-create.sh`) +
  idempotent bash bootstrap. Will graduate to Terraform when we have ≥2 boxes
  or want push-button rebuilds.
- **bearer-token auth on by default.** `API_TOKEN` is generated by bootstrap
  and required on every request except `/health`. Disable by emptying it in
  `.env.prod` and re-running `infra/deploy.sh`.

### Bugs caught and fixed during the first deploy

These all bit during real bootstrap; recording so the next deploy doesn't.

1. **`worker.depends_on.ollama` errored out as "undefined service"** when
   `ollama` was profile-gated to `local-inference` and that profile wasn't
   active. Compose v2 doesn't auto-prune depends_on for profiled services.
   Fix: drop the dep entirely (worker connects to ollama lazily and prod
   runs mock-mode). Commit `b890ac3`.
2. **Compose v2 *appends* port lists across base + overlay** instead of
   replacing. Result: every internal service tried to bind both
   `0.0.0.0:PORT` (from base) and `127.0.0.1:PORT` (from prod overlay) and
   failed with "address already in use". Fix: `ports: !override` in the
   prod overlay. Compose ≥ 2.24 supports this. Commit `6d3cf06`.
3. **Bootstrap couldn't `git clone` from a private repo** anonymously.
   Fix: bootstrap now supports `git@github.com:` URLs and expects a
   read-only deploy key at `/root/.ssh/github_deploy` (placed
   out-of-band by `scp` before bootstrap runs). Persists into
   `core.sshCommand` so `infra/deploy.sh` keeps working without env-var
   plumbing. Commit `138f5f4`.

### Gotchas worth remembering

- **`uid=lxd` (999) inside a redis:7-alpine container is normal.** The
  redis user inside the alpine image happens to be uid 999, which on
  the host's `/etc/passwd` resolves to the dormant `snap.lxd` system
  user. Looks alarming in `ps`; isn't.
- **`bootstrap.sh` curl one-liner is now broken** while the repo is
  private. Run-from-VPS workflow is `scp bootstrap.sh + deploy key` →
  run locally on the box. Public again later → restore the one-liner.
- **Hetzner CPX21 has no GPU.** Don't try to enable real inference on
  the VPS. Real inference comes from external workers.
- **Compose `!override` requires v2.24+.** Hetzner's docker-compose-plugin
  on Ubuntu 22.04 ships the right version (verified v5.1.3).

### How to redeploy after a code change

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.253.156
sudo /opt/gamerai/infra/deploy.sh
```

That's `git pull && docker compose up -d --build`. Idempotent.

### How to rebuild the box from scratch

```bash
# Local: provision VPS (idempotent — re-uses existing if name matches)
infra/hcloud-create.sh

# Local: copy deploy key + bootstrap
scp -i ~/.ssh/id_ed25519_gamerai \
  ~/.ssh/gamerai-github-deploy \
  root@<IP>:/root/.ssh/github_deploy
scp -i ~/.ssh/id_ed25519_gamerai \
  infra/bootstrap.sh root@<IP>:/root/bootstrap.sh

# VPS:
ssh -i ~/.ssh/id_ed25519_gamerai root@<IP>
sudo /root/bootstrap.sh \
  --domain ai.dallinlayton.com \
  --email hyrumdrums@gmail.com
```

### Where the API_TOKEN lives

Generated by `infra/bootstrap.sh` into `/opt/gamerai/.env.prod` (mode 600).
Only on the box. If you lose it, rotate:

```bash
ssh -i ~/.ssh/id_ed25519_gamerai root@5.161.253.156 \
  'sed -i "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" /opt/gamerai/.env.prod && /opt/gamerai/infra/deploy.sh'
```

Then update every client (Windows agent's `config.json`, web/CLI clients).

---

## 2026-05-06 — Pitch branch merged into MVP branch

Merged 9 commits from `claude/add-startup-pitch-7SKv9` into the MVP branch.
+3133 lines, 32 files. Big additions:

- `business.md` — startup pitch
- `docs/ai-for-beginners.md`, `docs/project-gaps.md`
- `research/big-models-feasibility.md`
- `infra/` — VPS deploy kit (bootstrap, Caddy, prod overlay)
- `LICENSE`, `.github/workflows/ci.yml`, `requirements-dev.txt`
- `shared/auth.py` — bearer-token auth, gated by `API_TOKEN`
- `coordinator/idempotency.py`, `coordinator/rate_limit.py`,
  `coordinator/model_registry.py` + tests + e2e tests

### Conflicts and how they were resolved

- `client/web.py` — pitch branch wrapped every coordinator call in a `_client()`
  helper that injects `auth_headers()`. Local branch had a wholesale dashboard
  rewrite that bypassed it. Resolution: kept the dashboard rewrite, layered
  the `_client()` helper into every endpoint (including the new `/earnings`
  call). All proxy endpoints now go through auth.
- `docker-compose.yml` — pitch added `API_TOKEN` env vars to coordinator/worker/
  client; local added an `ollama` service. Auto-merge clean except for the
  worker block, where local removed `extra_hosts: host.docker.internal` (no
  longer needed once ollama is in-cluster) but pitch added `API_TOKEN` env.
  Kept both sides.
- `.gitignore` — kept both `ollama-models/` (local) and `.env.prod` (pitch).

### Tests after merge

44/44 pass (auth, coordinator e2e, idempotency, model_registry, rate_limit).

---

## Earlier — MVP foundations

Before this log existed, the MVP was built up across three commits:

- `1165ab4` — initial distributed-inference MVP (coordinator, worker, client,
  redis-backed queue, basic earnings calc).
- `b537813` — SQLite store, scheduler, web UI, observability, gamer-realism
  knobs (cold-start delays, network jitter, availability windows).
- `c5128fa` — Windows worker agent (gamer install simulation).

Then the parallel `claude/add-startup-pitch-7SKv9` branch added auth, infra/,
docs, and tests (see merge entry above).
