# Can GamerAI Match Frontier-Class Models?

> Research note. Asks: can a network of idle gaming PCs serve models in the
> Llama-405B / DeepSeek-V3 / Mixtral-8x22B class — i.e., the things people
> actually pay OpenAI and Anthropic for — or are we structurally limited to
> the Llama-3.2-1B tier?
>
> Short answer: **yes, but only with the right architecture**. The literature
> and shipping projects (Petals, EXO, Prime Intellect) show frontier-class
> inference on consumer GPUs over the public internet is already real. It is
> slower and bandwidth-constrained, but tractable for the workloads we are
> already targeting (async, batch, agentic).

---

## 1. The core constraint

A modern frontier model has two numbers that matter:

| Model                      | Total params | Active per token | INT4 weights      |
| -------------------------- | ------------ | ---------------- | ----------------- |
| Llama 3.1 8B               | 8B           | 8B (dense)       | ~5 GB             |
| Llama 3.1 70B              | 70B          | 70B (dense)      | ~40 GB            |
| Mixtral 8x22B              | 141B         | 39B (MoE)        | ~80 GB            |
| Llama 3.1 405B             | 405B         | 405B (dense)     | **200–250 GB**    |
| DeepSeek-V3 / R1           | 671B         | **37B** (MoE)    | ~380 GB total, but only ~20 GB active per token |

A single RTX 4090 holds 24 GB of VRAM. A 5090 holds 32 GB. So:

- 8B and 70B models fit on 1–2 consumer GPUs trivially.
- 405B-class **dense** models do not fit on any consumer GPU and do not fit
  on any reasonable cluster of them without sharding the weights themselves.
- **MoE models are the cheat code.** DeepSeek-V3 has 671B parameters total
  but only activates 37B per token via 8-of-256 expert routing. The
  active-parameter footprint is in the same ballpark as a 70B dense model.

This is the single most important fact for our strategy: **the industry
shifted to MoE precisely because it makes huge models tractable on
distributed, memory-limited hardware.** That shift works in our favor.

---

## 2. How distributed inference of huge models actually works

There are three ways to split a model across machines. They have very
different implications for residential networks.

### 2.1 Tensor parallelism (TP)

Shard each matmul across N GPUs; all-reduce after every layer.

- Communication: massive, every layer, every token.
- Latency-sensitive. Synchronizes after every layer; at TP=4 over PCIe,
  systems already spend more time on data transfer than on compute.
- **Verdict for residential internet: dead.** TP only works on a single
  host with NVLink/PCIe, or within a datacenter with InfiniBand. Not for us.

### 2.2 Pipeline parallelism (PP) — also called "layer sharding"

Each worker holds a contiguous slice of the model's layers. Activations
flow through the pipeline like a bucket brigade.

- Communication: one activation tensor per layer-boundary per token.
  Roughly a few KB to a few MB per token, **not per layer**.
- Latency-tolerant. A 100ms hop between layer-groups is survivable.
- This is what Petals and EXO use. EXO calls it a "ring topology."
- **Verdict: this is the architecture for us.** It's already shipping in
  multiple open-source systems over residential internet.

### 2.3 Expert parallelism (EP)

For MoE models specifically: each worker hosts a subset of the experts.
For each token, the router picks 8 of 256 experts, and only the workers
holding those experts do work.

- Communication: token → router → "send this token to expert workers
  [12, 47, 99, 113, ...]" → results back.
- Sparse activation means most of the network does nothing per token —
  perfect for a network with high supply variance.
- **Verdict: this is the killer fit for a gamer-GPU network.** A worker
  that hosts a few experts only lights up when the router needs them, so
  it can game and earn at the same time more easily than a TP/PP node.

### 2.4 What we can mix

A real production network would combine all three:

- **TP within a host** (a gamer's two 4090s become one TP=2 node).
- **PP across hosts on the same continent** (low-latency layer pipeline).
- **EP for MoE models** (sparse expert routing across the wider network).

This is exactly the layered topology Prime Intellect describes for their
"planetary-scale inference engine."

---

## 3. What's already been shown to work

Real systems, real numbers, all on consumer/heterogeneous hardware.

### Petals — BitTorrent for LLMs

- Runs **Llama 3.1 405B**, Mixtral 8x22B, Falcon 180B, BLOOM-176B over
  the public internet on volunteer consumer GPUs.
- Single-batch throughput: **6 tok/s for Llama 2 70B, 4 tok/s for
  Falcon 180B** in geographically-distributed setups.
- Key claim: "3–25× faster latency than CPU offloading" — i.e., for any
  model that doesn't fit in your local VRAM, joining a Petals swarm is
  better than disk-offloading.
- **Implication for us:** the math has already been validated. A 405B
  model can serve interactive-grade traffic from consumer GPUs over the
  public internet today.

### EXO Labs — heterogeneous clusters

- Open-source ring-topology layer-sharding across mixed hardware: 2x
  RTX 3090s, desktop + Mac Studio, even smartphones in the cluster.
- Recent 2026 demo: 2x DGX Spark + M3 Ultra Mac Studio in a single
  cluster, 2.8–4× speedup over the Mac Studio alone on Llama 3.1 8B.
- Notable: the cluster mixes wildly different bandwidth/compute profiles
  (Spark: 273 GB/s mem, 100 TFLOPS; M3 Ultra: 819 GB/s mem, 26 TFLOPS)
  and the framework auto-balances. **This is the same heterogeneity
  problem we'll face with consumer gaming GPUs.**

### Prime Intellect — planetary-scale inference

- Explicit design target: "consumer GPUs and the 100ms latencies of the
  public internet."
- Trained INTELLECT-2 (32B reasoning model) across a decentralized
  network using globally distributed RL — not just inference, **training**.
- Generated SYNTHETIC-2 (4M reasoning traces) via pipeline-parallel
  decentralized inference: **1,250 GPUs from 4090s to H200s, joined in 3
  days**. Demonstrates the supply curve we're betting on.
- Two pieces of their stack are directly relevant to GamerAI's open
  problems:
  - **TOPLOC** — proof-of-inference scheme for verifying that a
    distributed worker actually ran the requested model honestly. This
    is exactly the "no proof-of-work" gap in our README's limitations.
  - **SHARDCAST** — efficient weight broadcast to a fleet of inference
    workers. We need this to push new models out to gamers without
    saturating their uplinks.

### Distributed speculative decoding

- Recent work (GoodSpeed, TSLT, et al.) extends speculative decoding to
  distributed settings: a small draft model proposes tokens, a large
  target model verifies them in parallel.
- Reported speedups: 2–3× end-to-end on Llama 3.1-70B with a 1B draft.
- "Truncated Sparse Logits Transmission" (2025) cuts the verification
  uplink from full-vocab logits to a small candidate set, making
  distributed speculative decoding feasible over residential bandwidth.
- **Why this matters for us:** latency is our biggest disadvantage vs.
  hyperscalers. Speculative decoding is the cheapest published lever to
  close that gap, and it composes naturally with pipeline parallelism.

---

## 4. The honest residential-internet bottleneck

Every paper on the topic agrees that **bandwidth, not compute, is the
limit** for distributed inference outside a datacenter. Specifics:

- A typical US residential uplink is 10–50 Mbps. Even gigabit fiber
  homes upload at maybe 200–500 Mbps real-world.
- Pipeline-parallel activation tensors per token: roughly the model's
  hidden-dim × bytes — ~16 KB to a few MB per layer boundary per token,
  depending on model and dtype.
- That's tractable for **batch 1 streaming generation** at single-digit
  tok/s. It is not tractable for the 1000+ tok/s aggregate throughput a
  hyperscaler API delivers — at least not from a single pipeline.

The implication is that a gamer-GPU network does not match a hyperscaler
on **single-stream interactive latency** for chat. It matches them on:

- **Aggregate throughput** (many independent pipelines in parallel).
- **Async / batch workloads** where a few seconds of extra latency is
  free.
- **Cost** ($/1M tokens), because the marginal compute is already paid
  for by the gamer's electric bill.

This maps cleanly onto what we already say in our pitch ("target async +
batch first") and in the README ("higher latency than centralized
providers — honest").

---

## 5. So can we match "big AI features"?

Reframing the question by feature:

| Feature                                    | Can we match it?  | How                                                                                  |
| ------------------------------------------ | ----------------- | ------------------------------------------------------------------------------------ |
| Frontier-class model quality (405B / V3)   | **Yes**           | Pipeline parallelism + MoE expert parallelism. Petals already does it.               |
| Long context (128k–1M tokens)              | **Yes, slower**   | Context-parallel sharding adds another dimension; latency cost scales with context.  |
| Tool use / function calling                | **Yes**           | This is a prompt-formatting problem, model-agnostic. Free.                           |
| Vision (multimodal in)                     | **Yes**           | Llama 3.2 Vision, Pixtral, Qwen2-VL all run on consumer GPUs and shard like LLMs.    |
| Code generation at frontier quality        | **Yes**           | DeepSeek-V3 / Coder, Qwen3-Coder are open and competitive with Claude/GPT for code.  |
| Reasoning ("o1-style" long thinking)       | **Yes**           | DeepSeek-R1, INTELLECT-2 — open reasoning models exist in the same ballpark.         |
| Sub-second TTFT for chat                   | **No (today)**    | Residential RTT alone is 30–80 ms; pipeline depth multiplies this. Speculative dec helps. |
| 1000+ tok/s aggregate per customer         | **Yes (sharded)** | Many parallel pipelines. Per-pipeline tok/s stays modest.                            |
| Image gen (Flux, SDXL, etc.)               | **Yes**           | Single-GPU workloads — embarrassingly parallel across our worker pool.               |
| Voice (Whisper, TTS)                       | **Yes**           | Same — fits on one consumer GPU per worker.                                          |
| Realtime voice / video                     | **Hard**          | RTT-sensitive. Possible only if we can route to a nearby low-latency worker pool.    |
| Fine-tuning / LoRA training as a service   | **Yes**           | Petals already supports this; Prime Intellect does globally distributed training.    |

Net: **the things we cannot match are the latency-bound interactive
features.** The things we can match are everything else, which is a much
bigger market by token volume than realtime chat. Anthropic's batch API
exists for a reason.

---

## 6. Architecture changes this implies for GamerAI

If we want to extend the current MVP toward this, the minimum delta is:

1. **Worker tiers.** A worker advertises capability: VRAM, bandwidth,
   stability, locale. The coordinator routes jobs to workers that can
   actually hold the requested model's slice.
2. **Model shards as first-class.** Today a worker hosts one Ollama
   model whole. To serve 405B, the worker hosts a *layer range* or an
   *expert subset* and the coordinator knows which.
3. **Pipeline groups.** The scheduler binds N workers into an ephemeral
   "pipeline group" for the lifetime of a job (or longer, for warmth).
   Health and reaper logic extends to the group, not the single worker.
4. **Activation routing.** Currently jobs are pure HTTP request/response
   to one worker. Pipeline parallelism needs worker-to-worker activation
   passing. WebSockets or QUIC, with the coordinator only on the control
   plane.
5. **Result verification (TOPLOC-style).** Once we route a job across N
   strangers' machines, "trust the worker" is no longer acceptable.
   Either replicate the head/tail layers across two workers and compare,
   or adopt a published proof-of-inference scheme.
6. **Weight distribution (SHARDCAST-style).** Workers can't each
   download 200 GB of weights from S3. We need a peer-to-peer weight
   distribution layer.
7. **Speculative decoding hook.** A small draft model on the coordinator
   (or on a single nearby worker) proposes tokens; the pipeline only
   verifies. Cheapest single intervention for closing the latency gap.

None of this is novel research. All of it is shipping in at least one
open-source project today. Our differentiator is not the inference
engine — it's the **marketplace, payouts, gamer realism, and trust
model** wrapped around it.

---

## 7. What we should actually do near-term

In priority order, with rough effort:

- **(S) Add a "worker capability" field** to registration: VRAM, model
  list, bandwidth class. Cheap, makes the rest possible. ~1 day.
- **(S) Add a model registry to the coordinator.** Right now the model
  is a per-job string. Make it a typed ID with metadata: dense vs MoE,
  shard plan, min VRAM, license. ~1 day.
- **(M) Pilot Petals integration as a backend.** Don't reinvent pipeline
  parallelism — wrap Petals as a worker type and route 70B+ jobs to it.
  Lets us claim "frontier-model support" without writing the hard parts.
  ~1–2 weeks.
- **(M) Add a draft-model speculative-decoding option** for big-model
  jobs. Single-worker, single-GPU change. ~1 week.
- **(L) Ship pipeline-group scheduling** as a first-class coordinator
  primitive (own pipeline, not Petals). Only worth doing once the
  Petals-backed flow proves there's demand. ~1–2 months.
- **(L) Implement TOPLOC-style verification or k-of-n consensus.**
  Required before any of this serves untrusted customers. ~1 month.

The S items are worth doing now regardless of the big-model bet — they
make the existing MVP more honest about what each worker can actually do.

---

## 8. Risks specific to the big-model strategy

- **Cold start is brutal.** Loading a 70B+ model into a worker takes
  minutes. Pipeline groups must stay warm across many jobs to amortize.
  This complicates the "gamer can leave at any time" promise.
- **Egress costs for the platform.** If we host the weights and gamers
  pull them, we pay the bandwidth. SHARDCAST/peer distribution is not
  optional at scale.
- **Per-token economics shift.** A 405B job uses ~50× the joules of a
  Llama-3.2-1B job. Our flat $/token rate has to become tier-aware
  before we serve big models, or we lose money on every request.
- **Legal/IP exposure.** Hosting Llama 3.1 405B weights on third-party
  consumer machines triggers Meta's license terms. Need to read those
  carefully (commercial use threshold, attribution, etc.) and pick a
  permissive default model (DeepSeek-V3 is MIT-ish, much cleaner).

---

## 9. Bottom line

The decentralized-gaming-GPU strategy is **not** structurally limited to
small models. The literature and three shipping open-source projects
(Petals, EXO, Prime Intellect) demonstrate that frontier-class inference
on consumer GPUs over the public internet is feasible today, with
caveats on interactive latency and aggregate throughput.

Our marketplace doesn't have to invent any of that infrastructure. The
right play is to **integrate one of the existing distributed-inference
backends (probably Petals) for big models** while keeping our current
single-worker Ollama path for small ones — and focus our differentiation
on payouts, the gamer experience, and trust/verification.

We can credibly offer: DeepSeek-V3 / R1, Llama 3.1 405B, Mixtral 8x22B,
Llama 3.2 Vision, Flux, Whisper — i.e., the open-model equivalents of
most "big AI features" customers actually buy — by Phase 2 of the
roadmap, not Phase 4.

---

## Sources

- [Petals: Collaborative Inference and Fine-tuning of Large Models (arXiv 2209.01188)](https://arxiv.org/abs/2209.01188)
- [Distributed Inference and Fine-tuning of Large Language Models Over The Internet (arXiv 2312.08361)](https://arxiv.org/html/2312.08361)
- [Petals project site](https://petals.dev/)
- [Petals on GitHub](https://github.com/bigscience-workshop/petals)
- [DeepSeek-V3 Technical Report (arXiv 2412.19437)](https://arxiv.org/abs/2412.19437)
- [DeepSeek-V3 671B specifications and VRAM requirements (apxml)](https://apxml.com/models/deepseek-v3)
- [Llama 3.1 405B INT4 hardware requirements (Hugging Face)](https://huggingface.co/hugging-quants/Meta-Llama-3.1-405B-Instruct-GPTQ-INT4)
- [Multi-GPU LLM Inference parallelism guide 2026 (PremAI)](https://blog.premai.io/multi-gpu-llm-inference-tp-vs-pp-vs-ep-parallelism-guide-2026/)
- [Scaling LLM Inference: TP, CP, EP — Engineering at Meta](https://engineering.fb.com/2025/10/17/ai-research/scaling-llm-inference-innovations-tensor-parallelism-context-parallelism-expert-parallelism/)
- [BloomBee: Distributed Generative Inference at Internet Scales (arXiv 2604.21072)](https://arxiv.org/abs/2604.21072)
- [EXO on GitHub](https://github.com/exo-explore/exo)
- [EXO Labs: DGX Spark + M3 Ultra clustering benchmarks (Tom's Hardware)](https://www.tomshardware.com/software/two-nvidia-dgx-spark-systems-combined-with-m3-ultra-mac-studio-to-create-blistering-llm-system-exo-labs-demonstrates-disaggregated-ai-inference-and-achieves-a-2-8-benchmark-boost)
- [Prime Intellect: Planetary-Scale Inference](https://www.primeintellect.ai/blog/inference)
- [INTELLECT-2 release: globally distributed RL training (Prime Intellect)](https://www.primeintellect.ai/blog/intellect-2-release)
- [INTELLECT-2 paper (arXiv 2505.07291)](https://arxiv.org/abs/2505.07291)
- [NVIDIA: Introduction to Speculative Decoding](https://developer.nvidia.com/blog/an-introduction-to-speculative-decoding-for-reducing-latency-in-ai-inference/)
- [GoodSpeed: Adaptive Speculative Decoding in Distributed Edge Inference (arXiv 2512.09963)](https://arxiv.org/html/2512.09963)
- [Speculative Decoding in Decentralized LLM Inference (arXiv 2511.11733)](https://arxiv.org/pdf/2511.11733)
- [Fast collaborative inference via distributed speculative decoding (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2949715925000782)
