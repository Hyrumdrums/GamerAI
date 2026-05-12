# Developer log

Chronological record of meaningful changes, decisions, and gotchas. Most-recent
entries on top. Skim for context before resuming work.

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
