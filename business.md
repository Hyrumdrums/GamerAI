# Startup Pitch: Decentralized AI Compute Network

## One-liner
We turn idle gaming PCs into a global, energy-aware **AI toolbox**—chat,
images, web-augmented answers, and more—delivered without massive data
centers.

## The Problem
AI demand is exploding—but the infrastructure model is breaking.

Hyperscale data centers (like the proposed Utah project backed by Kevin O'Leary) require:
- gigawatts of power
- massive land use
- heavy water consumption
- Communities are pushing back
- Energy supply is becoming the bottleneck—not chips

At the same time:
- Millions of high-end GPUs sit idle in gaming PCs every day
- Developers face:
  - high API costs
  - infrastructure complexity
  - limited access to scalable compute

## The Solution
A distributed AI compute marketplace.

We connect:
- Idle GPUs (supply) → gamers, prosumers
- AI workloads (demand) → developers, startups

Through a lightweight agent, users opt in to contribute compute when idle.

## How it works
```
Developer → API request → Coordinator
                         ↓
              Distributed GPU network
                         ↓
                   Response returned
```

- Jobs are routed dynamically
- Workers are paid per task
- System scales organically with supply

## Why now
Three trends converge:
1. AI demand outpacing infrastructure
2. Rising resistance to hyperscale data centers
3. Huge untapped edge compute (gaming GPUs)

## Business Model
- Charge developers per token (e.g. $3–$8 / 1M tokens)
- Pay workers ~60–70% of revenue
- Platform keeps margin

## Target Customers

**Phase 1**
- indie developers
- startups
- cost-sensitive AI workloads

**Phase 2**
- batch processing pipelines
- document processing
- embeddings at scale

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

| Option | Cost | Setup | Power |
| --- | --- | --- | --- |
| Local models (e.g. Llama 3.2 1B) | low | medium | low |
| OpenAI / Anthropic | high | low | very high |
| Us | low | low | medium-high |

We sit in the middle: more powerful than local, cheaper than cloud.

## The Bigger Vision
We're not just a compute network. We're building:

**Energy-aware AI infrastructure**
- Jobs routed to where energy is cheapest or most available
- Compute scales without building new data centers
- Turns wasted energy + idle GPUs into useful work

**Distributed efficiency**
- Heat stays in homes instead of data centers
- No massive cooling infrastructure
- No single point of failure

## Strategic Advantage
Hyperscalers scale by building bigger.
We scale by connecting what already exists.

## Roadmap

**Phase 1 (done)**
- Local MVP (Docker + workers)
- Basic job routing + payouts

**Phase 2 (now)**
- Public single-VPS deploy (live at ai.dallinlayton.com — see `docs/devlog.md`)
- Real GPU workers via the Windows agent

**Phase 3**
- AI toolbox foundations: `job_type` + capability routing + web search +
  image generation
- Marketplace dynamics: per-tier pricing, reputation, payouts

**Phase 4**
- Energy-aware routing
- Global scaling

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
