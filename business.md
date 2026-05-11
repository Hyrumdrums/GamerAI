# Startup Pitch: Community-Powered AI Network

## One-liner
Plug your gaming PC into a community AI network. You contribute idle compute
to the shared pool; you and the people you invite get tier-based access to
the network's full AI suite — chat, images, web-augmented answers, and more.
Paying customers (later) help fund the contributors who serve them.

## Core mechanic — contribute-to-use
Contributing compute is the membership requirement. When you run the agent,
your GPU serves jobs from the network's shared queue anonymously — not just
your own invitees. In return, you earn tier-based access (BRONZE → PLATINUM)
that scales with your uptime and what your hardware can run. No subscription,
no per-token billing, no data leaving for OpenAI's data center.

## The Problem
AI demand is exploding — and the infrastructure model is breaking.

Hyperscale data centers (like the proposed Utah project backed by
Kevin O'Leary) require:
- gigawatts of power
- massive land use
- heavy water consumption
- Communities are pushing back
- Energy supply is becoming the bottleneck — not chips

At the same time, households and friend groups are stacking subscriptions:
- $20/month per seat on ChatGPT Plus across the whole household
- Image generation behind another paywall (Midjourney, etc.)
- Coding assistants behind another (Copilot, Cursor)

Meanwhile **millions of high-end GPUs sit idle in gaming PCs every day**
that could serve a whole friend group's everyday AI needs.

## The Solution
A contribute-to-use community network with an optional paid layer.

- **Contributors** (gamers, prosumers) plug their GPU into the network.
  Their machine serves the network's shared queue when idle.
- **Invitees** (their household, friends, family) get access without
  needing their own hardware; the contributor sets each invitee's cap.
- **Paid customers** (later, optional) — households without a gaming PC,
  app developers, enterprises — pay for access. Most paid revenue flows
  back to PRO/PLATINUM contributors who opt into serving paid jobs.

The contributor's GPU is the only supply. The platform never extracts
from contributor activity — only from paid activity, with a capped share
that funds coordinator infrastructure.

## How it works

```
Contributor agent → idle GPU →┐
                              │
                              ▼
              Coordinator (shared job queue)
                              ▲
                              │
    Invitee / contributor / paid customer
                              │
                              ▼
                        Response returned
```

Three loops, layered:

1. **Free access loop.** Contributor runs the agent → earns BRONZE →
   PLATINUM tier → tier sets quota + invite slots → contributor and
   their invitees consume the suite for free.
2. **Anti-freeloading.** Tier maintenance requires both uptime AND
   claimed-jobs-per-hour, so a contributor can't run a closed agent
   that only serves their own friends. Modeled on BitTorrent ratios.
3. **Paid revenue loop (Phase 3b+).** Paid customers buy access; their
   jobs go to a priority queue served only by opt-in GOLD+ contributors.
   80% of paid revenue → contributors who served the jobs; 20% → platform
   (coordinator + future development).

## Why now
Three trends converge:
1. AI demand outpacing infrastructure
2. Rising resistance to hyperscale data centers
3. Huge untapped edge compute (gaming GPUs)
4. **Household AI subscription fatigue** — paying for ChatGPT, Midjourney,
   and Copilot per seat across a family adds up fast when one contributor's
   gaming PC could serve them all.

## The supply-demand challenge (and how we balance it)

Paid demand and gamer supply are **anti-correlated** in a single time
zone — paid demand peaks 9–6 weekdays, gamer supply peaks overnight
and weekends. Three structural levers balance the curve:

1. **BATCH paid tier** (above) lets non-urgent paid work fill off-peak
   supply at half price.
2. **Geographic contributor recruiting** — EU and APAC contributors fill
   US business hours; same shape in reverse. The "advertise for more
   supply" motion is geo-specific, not generic.
3. **Utilization-driven signals** — when network use rises, a dashboard
   alert ("Usage is growing — consider adjusting your uptime to reach
   the next tier") nudges offline GOLD+ contributors to spin up.
   When use is low, paid-customer acquisition (dev forums,
   comparison pages, BATCH campaigns) drives demand.

Two acquisition motions, one signal loop: **low utilization triggers
paid-customer marketing; high utilization triggers contributor
recruiting.** The audiences barely overlap.

## Business Model

The economics are layered:

**Layer 1 — Free contributor-tier access (MVP).** Tiers earned by
contribution; no money changes hands. The contributor is the primary
user, not a labor force.

**Layer 2 — Paid customer tiers (Phase 3b+).**
- **CASUAL** — households without a gaming PC, flat monthly fee
- **DEVELOPER** — realtime per-token API, ~$1.50/1M tokens (between
  Haiku $1.25/1M and self-hosted)
- **BATCH** — non-realtime per-token, ~$0.75/1M tokens, scheduled into
  low-utilization windows (the supply-soak lever; lets paid customers
  fund off-peak supply at half price)
- **ENTERPRISE** — volume + SLA + dedicated worker pools + privacy-tier
  routing

**Layer 3 — Bonus payouts.** 80% of paid revenue → opt-in GOLD+
contributors who actually served the paid jobs (per-token). 20% →
platform for coordinator infra and future development. Tier promotion
is instant (low friction); paid-pool eligibility requires 1 week of
sustained uptime + minimum claim rate (reliability proof).

**Realistic contributor earnings** (US-median electricity, after
electricity cost, 80% worker share):

| GPU | 1 hr/day active | 3 hr/day | 8 hr/day saturated |
|---|---:|---:|---:|
| Basic (RTX 3060) | $3/mo | $9/mo | $24/mo |
| Mid (RTX 4070) | $8/mo | $22/mo | $58/mo |
| High (RTX 4090) | $11/mo | $32/mo | $87/mo |

Margin per million tokens served stays at 60–90% even on basic GPUs in
expensive electricity. The community pull (status, free suite, host for
your people) is the primary contributor motivation; bonus payouts are
the cherry on top — meaningful at the high end, "Netflix-sub money"
on basic gear.

**Sustainability target:** at $1.50/1M tokens to DEVELOPER customers,
two paying developers + one PLATINUM contributor in the paid pool covers
the coordinator bill (~$50/mo at 1k users). Not a unicorn target — a
year-one milestone that gets the founder off self-hosting at a loss.

## Target Customers

**Foundation: contributors and their invitees**
- gamer households
- friend groups, D&D groups, college dorms, coworking spaces
- small organizations where one person can host

**Paid customer segments (Phase 3b+)**
- **CASUAL** — households without a gaming PC (replacement for
  per-seat ChatGPT Plus)
- **DEVELOPER** — indie devs, startups, cost-sensitive AI workloads
- **ENTERPRISE** — batch processing, document workflows, embeddings at
  scale, regulated industries via the privacy tier

## Supported tools

The platform is positioned as an "AI toolbox," not a single-purpose chat
API. The tool list is constrained by what a heterogeneous consumer-GPU
network can actually serve well: independent jobs, interruptible/retryable,
moderate-latency tolerant.

**MVP launch (3 tools)**
- **Chat** — 7B–13B baseline (Llama 3 8B-class). The default surface.
- **Image generation** — SDXL-class, ~8–12 GB VRAM. Strong demo, async-friendly.
- **Web-augmented answers** — centralized search + local-model summarization.
  Cheap to add, biggest perceived-intelligence boost for small models.

**Expansion (when MVP is stable)**
- Document tools (summarize, rewrite, chunked analysis) — high retention.
- Coding assistant (autocomplete, small-function generation, debugging help).

**Secondary (later)**
- Music generation (MusicGen) — async/queued only.
- Voice (batch STT/TTS) — real-time voice deliberately out of scope.

**Not on the roadmap**
- Video generation at scale, large tightly-coupled multi-node inference,
  real-time low-latency systems, frontier-model training. Each violates
  the "loosely coupled, retryable, latency-tolerant" constraint.

A unified UI abstracts this list — users chat, drop in a `/image` command,
or ask a question that triggers a search-augmented answer. The backend
routes each job to a worker advertising the matching capability.

## Competitive Positioning

| Option | Cost to user | Setup | Quality | Privacy |
| --- | --- | --- | --- | --- |
| Local-only (Ollama on your own machine) | Free, but power costs | High | Limited by your own hardware | Best |
| OpenAI / Anthropic / etc. | High ($20/seat/mo) | None | Top tier | Worst (trains on prompts unless you pay extra) |
| **Us** | **Free for contributors and their invitees; modest for paid customers** | **Run an agent, or get invited** | **8B–13B baseline today; pipeline to bigger models on the roadmap** | **Community network — no hyperscaler in the path** |

We don't compete with OpenAI on quality at the frontier. We compete on
*"my friend runs this for our group"* — and on the household-subscription
fatigue that's compounding across every consumer AI category.

## The Bigger Vision
We're not just a compute network. We're building:

**Community-owned AI infrastructure**
- Hyperscalers can't be your friend's GPU
- Compute scales without building new data centers
- Turns wasted energy + idle GPUs into household-scale AI

**Distributed efficiency**
- Heat stays in homes instead of data centers
- No massive cooling infrastructure
- No single point of failure
- Energy-aware routing (Phase 4): jobs go where electricity is cheapest
  or cleanest at that moment

## Strategic Advantage
Hyperscalers scale by building bigger.
We scale by **connecting people who already have idle GPUs to people who
already trust them**. The community is the moat: incumbent providers
can't be your D&D group's host, your family's host, your coworking
space's host. We can.

## Roadmap

**Phase 1 (done)**
- Local MVP (Docker + workers)
- Basic job routing + payouts ledger

**Phase 2 (now)**
- Public single-VPS deploy validated (real Llama-3.2:1b on CPU, then
  Llama-3.1:8b on a real consumer GPU at ~50 tok/s — see `docs/devlog.md`)
- Real GPU contributors via the Windows agent

**Phase 3**
- 3a — AI toolbox foundations: `job_type` + capability routing + web
  search + image generation
- 3b.i — Membership and tier engine: invite flow, tier promotion engine,
  per-tier quotas, Alice→Bob invitee mechanic, host admin UI
- 3b.ii — Paid customer layer: paid-job priority queue, per-tier paid
  pricing, Stripe Connect for contributor bonuses, opt-in pool for
  GOLD+ contributors
- 3b.iii — Trust & verification: dynamic pricing, reputation scoring,
  challenge jobs, customer dashboards

**Phase 4**
- Big-model support (Petals → EXO) for frontier-class workloads
- Energy-aware routing
- Global scaling

**Phase 5**
- Privacy tiers, especially client-side embedding for sensitive prompts
  and enterprise customers

## Risks (and why we still win)
- **Latency** → Target async + batch workloads first
- **Reliability** → Redundant job routing
- **Trust** → Transparent agent + open model

## Vision Statement
AI shouldn't require building power plants.

We're creating a world where:
- compute is distributed
- energy use is adaptive
- and anyone can participate in the AI economy

## Closing
The future of AI isn't just bigger data centers.

It's millions of machines working together—efficiently, opportunistically, and globally.
