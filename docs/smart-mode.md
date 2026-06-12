# Smart mode — pooling two machines' VRAM for a 14B-class chat model

Smart mode lets two GamerAI machines on the same private network —
same LAN, or any two peers linked by an overlay like Tailscale (see
"Pairing over the internet" below) — serve a chat model neither GPU
can hold alone. The reference deployment is the founder's
pair: a 6 GB card and an 8 GB card, which together comfortably fit
**Qwen2.5-14B-Instruct Q4_K_M** (~9 GB of weights + KV cache) — roughly
4–5× the parameter count of the network's 3B canonical, and a whole
capability class up in reasoning, instruction-following, and coding.

It is slower than a flagship hosted model and that's fine — the value
proposition is "a much smarter answer from hardware you already own,"
not latency parity with ChatGPT. Expect single-digit tokens/sec
depending on the LAN and the cards.

## How it works

```
            chat (smart) job, via job_queue:chat:smart
                              │
                              ▼
   ┌────────────────────── HEAD (8 GB) ──────────────────────┐
   │ agent ──► llama-server  -m qwen2.5-14b.gguf             │
   │           --rpc 192.168.1.42:50052   (OpenAI API on     │
   │           holds ~layers 0..N locally  127.0.0.1:8092)   │
   └────────────────────────────┬─────────────────────────────┘
                                │ activations over LAN (RPC)
   ┌────────────────────── BACKEND (6 GB) ────────────────────┐
   │ agent ──► rpc-server -H 0.0.0.0 -p 50052                 │
   │           holds the remaining layers in its VRAM         │
   └───────────────────────────────────────────────────────────┘
```

- It uses **llama.cpp's RPC backend** ([docs](https://github.com/ggml-org/llama.cpp/tree/master/tools/rpc)):
  the head's `llama-server` treats the backend's GPU as one more
  device and splits the model's layers across both **in proportion to
  free VRAM** (override with `tensor_split`). Activations — a few KB
  per token, not weights — cross the LAN per layer boundary, which is
  why this works fine on gigabit Ethernet and tolerably on good Wi-Fi.
- Coordinator-side, smart jobs keep `tool="chat"` but ride a dedicated
  `job_queue:chat:smart` queue that **only the head polls** (it
  advertises the `chat:smart` capability). Streaming, partials,
  conversations, and earnings all reuse the existing chat plumbing.
- The web UI grows a **"Smart mode"** toggle in the composer (sticky
  per conversation, like search). It sends `smart: true` on
  `/generate`; the coordinator resolves that to the registry's
  `DEFAULT_SMART_MODEL` and routes accordingly. If the pipeline is
  offline the user gets a clear 503 message instead of a queued-forever
  job.

## Setting it up (two Windows machines, agents already paired)

Pick the machine with **more VRAM as the head** — it holds the bigger
layer share plus compute buffers, and it's the one running
`llama-server`.

### 1. Backend machine (the 6 GB box)

Edit `%APPDATA%\GamerAI\config.json` (or the config next to the agent):

```json
"smart": {
  "enabled": true,
  "role": "backend"
}
```

Restart the agent. First run downloads the pinned llama.cpp CUDA build
(~500 MB, one-time per release) and launches `rpc-server` on port
50052. Allow the inbound rule if Windows Firewall prompts — or add it
manually:

```
netsh advfirewall firewall add rule name="GamerAI smart rpc" dir=in action=allow protocol=TCP localport=50052 remoteip=localsubnet
```

Note the machine's LAN IP (`ipconfig` → e.g. `192.168.1.42`). Giving
it a DHCP reservation in your router avoids the IP drifting later.

> **Security note:** llama.cpp's RPC protocol is unauthenticated by
> design. `rpc_listen_host: 0.0.0.0` exposes it to your LAN only —
> never port-forward it to the internet. The `remoteip=localsubnet`
> firewall scope above is the belt-and-suspenders version.

### 2. Head machine (the 8 GB box)

```json
"smart": {
  "enabled": true,
  "role": "head",
  "rpc_peers": ["192.168.1.42:50052"]
}
```

Restart the agent. First run downloads the same llama.cpp build plus
the model GGUF (~9 GB — this is the long one), then launches
`llama-server`, which streams the backend's layer share over the LAN
on first load. **First startup can take several minutes**; the agent
logs progress and only advertises `chat:smart` once the server's
`/health` goes green. Subsequent loads are much faster (`rpc-server`
runs with its local tensor cache on, so the shard is re-read from the
backend's own disk).

### 3. Try it

Open the chat UI, tick **Smart mode**, ask something hard. The status
line says "thinking (smart mode is slower)…" and the answer streams
with the usual typewriter. The message is billed/credited in tokens
exactly like standard chat.

## What a smart-enabled agent stops doing

The pipeline shard owns the GPU's VRAM, so a smart-enabled agent
**does not** bootstrap Ollama or stable-diffusion, and advertises:

- head: `chat:smart` (+ `tts` — Piper is CPU-only)
- backend: nothing on the GPU (+ `tts` if bootstrapped)

The usual idle gates still apply: the agent only claims jobs when the
machine is idle, and the game-process / GPU-utilization checks keep it
out of the way of a gaming session. (The sidecar processes do keep
their VRAM resident while the agent runs — if you game on one of these
machines regularly, expect to see less free VRAM while the agent is
up. Killing the agent releases everything.)

## Testing with a smaller model

While refining smart mode you don't want a 9 GB download and
minutes-long loads on every iteration. The smart *tier* is
model-agnostic — point it at a smaller chat model and the whole path
(queue routing, head/backend pairing, streaming, requeue, UI toggle)
is exercised identically, just faster.

Two settings have to agree — the coordinator decides what "smart"
resolves to, the head decides what it loads:

**1. Coordinator** — set the `SMART_MODEL` env var and restart:

```bash
# in .env.prod (VPS) or the shell (local dev)
SMART_MODEL=qwen2.5:7b
docker compose up -d coordinator
```

This changes what `smart: true` resolves to AND makes that name route
to the smart queue even though its registry entry is standard-tier.

**2. Head agent** — match the model in `config.json` and restart:

```json
"smart": {
  "enabled": true,
  "role": "head",
  "model": "qwen2.5:7b",
  "rpc_peers": ["..."]
}
```

The agent has built-in GGUF sources for these names (anything else
needs an explicit `gguf_url`):

| `model` | download | fits |
|---|---|---|
| `qwen2.5:14b` | 8.99 GB | the production target — needs the two-machine pool |
| `qwen2.5:7b` | 4.68 GB | a single 6 GB or 8 GB card — **the recommended test model**: small enough to load fast, big enough that splitting it across two peers still exercises the RPC path for real |
| `llama3.2:3b` | 2.02 GB | anything — smoke-testing the plumbing when you don't care about answer quality |

Notes for test runs:

- A 7B/3B fits on one card, so you can even drop `rpc_peers` and run a
  **single-machine head** to test everything except the RPC hop itself.
- You can keep a backend paired anyway — llama.cpp happily splits a
  small model across both devices, which is the cheapest way to
  validate the peer link before committing to the 14B download.
- Each model gets its own GGUF under `%APPDATA%\GamerAI\llama\models\`,
  so switching back to `qwen2.5:14b` doesn't re-download anything you
  already have.
- Mid-conversation model swaps are fine: the smart flag is per-turn,
  and requeue routing re-derives from the model stamped on the job.

## Pairing over the internet (any two peers)

Same-LAN is **not** actually a hard requirement — the head connects to
whatever address is in `rpc_peers`. What IS a hard requirement is that
the link be private: llama.cpp's RPC protocol has no authentication or
encryption, so the backend port must never be reachable from the open
internet (anyone who can reach it can run compute on, and crash, that
GPU).

The supported way to pair two machines in different households today
is an overlay network — [Tailscale](https://tailscale.com) (free for
personal use, ~5 minutes, no router/port-forward config) or a WireGuard
tunnel you manage yourself:

1. Install Tailscale on both machines, sign both into the same
   tailnet.
2. On the backend, bind rpc-server to the tailnet interface instead of
   the whole machine, so the GPU is only offered over the encrypted
   link — set `rpc_listen_host` to the machine's Tailscale IP
   (`tailscale ip -4`, a `100.x.y.z` address).
3. On the head, use that same Tailscale IP in `rpc_peers`:
   `"rpc_peers": ["100.101.102.103:50052"]`.

That's it — the agents don't know or care that the bytes cross town
instead of the living room.

**Set expectations accordingly.** Pipeline parallelism sends
activations between peers for every token, so per-token latency picks
up the WAN round-trip:

- LAN (sub-1 ms RTT): the RPC hop is nearly free.
- Same-city WAN (10–30 ms RTT): noticeably slower generation —
  roughly, each token pays the RTT on top of compute.
- Cross-country (60–100 ms RTT): a few tokens/sec at best. Petals
  serves 70B-class models over the public internet at ~5–6 tok/s, so
  this degrades gracefully rather than falling over — but test before
  promising anyone a good experience.
- First model load streams the backend's layer share (~half the GGUF)
  over the link — tens of minutes on a residential uplink. The
  backend's local tensor cache (`-c`, on by default) makes that a
  one-time cost per model.
- Bandwidth is the easy part: activations are ~10 KB/token for the
  14B, well within any broadband uplink.

Longer term, "any two peers, brokered by the coordinator" — agents
advertise smart-pipeline capability, the coordinator matches peers,
exchanges keys, and the agents build their own authenticated tunnel
without the user installing anything — is exactly the Phase 4b
pipeline-groups work in `research/big-models-feasibility.md`. The
Tailscale path is how we validate demand before building that.

Part of that brokering should be **latency-aware matchmaking**: a
speed endpoint on the coordinator (or coordinator-directed agent-to-
agent pings) that measures RTT between smart-capable peers, so the
coordinator pairs the two *closest* nodes as the halves of a pipeline
rather than any two volunteers. Since per-token speed is dominated by
the RTT between the halves, picking neighbors is worth more than
picking the biggest GPUs.

## About the shard transfer (one-time, and why we don't pre-seed it)

**Is the first-load transfer one-time forever?** Effectively yes — per
model, per backend. The mechanics: in llama.cpp's RPC design the head
is the only process that ever reads the GGUF; on load it streams each
backend's tensors over the link. We run `rpc-server` with its tensor
cache on, and pin the cache to `%APPDATA%\GamerAI\llama\rpc-cache\`,
so every received tensor is stored on the backend's disk keyed by
content hash. On every later load the head sends hashes first and the
backend answers from disk — no re-transfer. The cache survives agent
restarts, agent self-updates, and reboots. You pay the slow transfer
again only when the tensors actually change: a different model, a
different quant, or a `tensor_split` change (which moves layers
between machines — and only the moved layers re-transfer).

**Why not pre-package A/B shard halves both agents download up front?**
It's a natural idea and we considered it; it doesn't fit how the
engine works, and forcing it would make things less simple, not more:

- `rpc-server` has no code path that reads model files. Giving the
  backend the GGUF (or a pre-cut half) gives it bytes it cannot use.
- The cache is keyed by llama.cpp-internal content hashes of tensors
  *as transformed at load time*. Pre-seeding it would mean
  re-implementing that hashing and layout outside llama.cpp — a
  fragile coupling that breaks silently on every release bump.
- A fixed A/B cut would also hard-code the split, losing the
  free-VRAM-proportional placement that makes mismatched cards (6+8,
  6+12, …) work without configuration.

The honest cost today is therefore: **one slow first load per model
per backend** (minutes on LAN, potentially an hour-class wait over a
residential WAN link), and near-instant loads forever after. That
matches the project's bias — simple and secure beats fast — and the
config already softens the first load (`startup_timeout_seconds`,
plus the smaller test models above for iteration).

## Config reference (`smart` block)

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | master switch |
| `role` | `"head"` | `"head"` (runs llama-server, claims jobs) or `"backend"` (runs rpc-server, lends GPU) |
| `model` | `"qwen2.5:14b"` | must match what the coordinator routes as smart: a smart-tier registry model, or the `SMART_MODEL` env override (see "Testing with a smaller model") |
| `rpc_peers` | `[]` | head: backend `ip:port` list. Empty = single-machine (spills to CPU, slow) |
| `rpc_listen_host` / `rpc_listen_port` | `0.0.0.0` / `50052` | backend bind address |
| `llama_release` | pinned tag | llama.cpp release. **Head and backend must match** (RPC protocol compatibility); the install dir is keyed by it |
| `llama_zip_url` / `cudart_zip_url` | `null` | override the GitHub release asset URLs (e.g. for a Vulkan build on AMD) |
| `gguf_url` | `null` | override the model download (default: bartowski's Q4_K_M single-file GGUF) |
| `context_length` | `8192` | pipeline-wide KV budget. Drop to 4096 if you OOM |
| `tensor_split` | `null` | e.g. `"5,8"` — manual layer proportions, order `[peers..., local]` |
| `llama_server_port` | `8092` | head's local OpenAI-compatible port (loopback only) |
| `extra_args` | `[]` | appended raw to the sidecar command line |
| `endpoint` | `null` | point at your own OpenAI-compatible server and skip managed launch entirely (also the non-Windows dev path) |
| `startup_timeout_seconds` | `1200` | head health-check budget for the first slow load |

## Troubleshooting

- **Head never goes healthy** → check `%APPDATA%\GamerAI\logs\llama-head.log`.
  Usual suspects: backend agent not running, wrong IP in `rpc_peers`,
  firewall blocking 50052, mismatched `llama_release` between machines.
- **OOM on load** → lower `context_length` to 4096, or bias more
  layers to the bigger card with `tensor_split`.
- **AMD / non-NVIDIA GPU** → the default zips are CUDA builds; point
  `llama_zip_url` at the matching Vulkan or HIP asset of the same
  release tag (no cudart needed for Vulkan — point `cudart_zip_url`
  at the same zip or leave the file absent and set it explicitly).
- **It's slow** → expected; check you're on Ethernet rather than Wi-Fi
  first, then experiment with `tensor_split`. Wired gigabit between
  the boxes is the single biggest lever.

## Where this goes next

This is the first concrete step of the Phase 4 plan in
`research/big-models-feasibility.md`: pipeline-parallel inference
across contributor machines. Today the pairing is static config on two
machines owned by one contributor; coordinator-side pipeline-group
scheduling (binding arbitrary contributors' machines into ephemeral
pipelines, EXO/Petals-style) is the follow-on once this proves out.
