# AI for Beginners

> A plain-English guide to the words, ideas, and machinery behind modern
> AI — written for someone who's smart but new to this. Read top to
> bottom, or jump to the glossary at the end.

---

## 1. What is "AI" today, really?

When people say "AI" in 2026, they almost always mean a **Large Language
Model (LLM)** — a program trained to predict the next word in a piece of
text. ChatGPT, Claude, Gemini, Llama, DeepSeek: all LLMs.

That sounds underwhelming, but predicting the next word *well enough* —
across billions of pages of human writing — turns out to produce
something that can write code, summarize documents, answer questions,
and hold a conversation. The system isn't "thinking" the way humans do.
It's pattern-matching at a scale we don't have good intuition for.

Two phases matter:

- **Training** — feeding the model trillions of words and adjusting its
  internal numbers until it gets good at predicting them. Costs millions
  to hundreds of millions of dollars. Done once.
- **Inference** — using the trained model to answer your questions.
  Cheap per query, but expensive at scale because every user query needs
  GPU time.

GamerAI is an **inference** marketplace. We don't train models; we run
already-trained ones for paying customers.

---

## 2. The 20 words you actually need

If you only learn this section, you can follow most AI conversations.

### Token

The unit an LLM reads and writes. Roughly **¾ of an English word**.
"Hello world" is 2 tokens. "antidisestablishmentarianism" is ~6 tokens.
Pricing is almost always per million tokens.

### Parameter (a.k.a. weight)

A single number inside the model. A "70B model" means 70 billion of
these numbers. More parameters = (usually) more capable, more expensive
to run.

### Model

The whole bundle of parameters plus the architecture (the recipe for
how they're connected). When you "download Llama 3.1 70B," you're
downloading ~140 GB of numbers.

### Inference

Running the model to get an answer. One question = one inference call.

### Context window

How much text the model can "see" at once — your prompt plus the
conversation so far. Measured in tokens. Modern models go from 8k
(Llama 2) to 1M+ (Gemini, Claude). Bigger context = the model can read
more documents in one shot, but each token costs compute.

### Prompt

What you send to the model. Includes your question, instructions, and
any context (documents, conversation history).

### Completion

What the model sends back. Generated one token at a time, fast enough
to look like streaming text.

### Temperature

A knob from 0 to ~2 controlling randomness. Temperature 0 = always pick
the most likely next token (deterministic, "boring" answers). Temp 1 =
default (varied but coherent). Temp 2 = chaotic.

### System prompt

A hidden instruction the model sees before your message. "You are a
helpful assistant. Be concise." Sets behavior for the whole conversation.

### Fine-tuning

Taking an already-trained model and giving it more training on your
specific data — e.g., teaching it your company's writing style.
Cheaper than training from scratch but still nontrivial.

### LoRA (Low-Rank Adaptation)

A clever cheap version of fine-tuning. Instead of changing all 70B
parameters, you train ~0.1% extra parameters that get added on top.
Tiny file, big effect. Most "custom" models you see are LoRAs.

### Quantization

Squashing the model's numbers into fewer bits to save memory.
- Original: 16 bits per parameter (FP16)
- 8-bit (INT8): half the size, almost no quality loss
- 4-bit (INT4): a quarter the size, slight quality loss
- 2-bit: notable quality loss but lets you run huge models on small GPUs

A 70B model is 140 GB at FP16, 35 GB at INT4. Quantization is what
makes consumer-GPU inference practical at all.

### Embedding

Converting text into a list of numbers (a "vector") that captures its
meaning. Used by **search**, **RAG** (see below), and as the very first
step inside an LLM. Two pieces of text with similar embeddings have
similar meanings.

### RAG (Retrieval-Augmented Generation)

The trick where, before answering your question, the system searches
a database of documents, finds the relevant ones, and stuffs them into
the prompt. This is how "chat with your PDF" tools work. Doesn't
require retraining the model — just better prompting.

### Agent

An LLM in a loop, allowed to **call tools** (search the web, run code,
read files, send email). The model decides what to do next, runs it,
sees the result, decides again. Claude Code is an agent. ChatGPT with
"actions" is an agent.

### Tool use / Function calling

The mechanism by which an agent talks to the outside world. The model
emits a structured request like `search_web("rainfall in Utah 2025")`,
the runtime executes it, the result goes back into the model's context.

### Multimodal

A model that handles more than just text. Vision (images in), audio
(speech in/out), sometimes video.

### Reasoning model

A newer class (OpenAI o1, DeepSeek-R1, Claude with extended thinking)
that "thinks" before answering — generates a long internal chain of
reasoning, then writes the final response. Slower and pricier, much
better at math, code, and logic puzzles.

### Open-weights vs. closed

- **Closed:** you can only use the model through the company's API.
  GPT-4, Claude, Gemini are closed.
- **Open-weights:** the parameter file is downloadable. You can run it
  yourself. Llama, Mistral, Qwen, DeepSeek, Mixtral are open-weights.

"Open-source" is loose terminology in AI; most "open" models release
weights but not their training data, so purists call them
"open-weights" rather than truly open-source.

### Hallucination

When a model confidently states something false. Not a bug to be
patched — it's intrinsic to how prediction works. Mitigated (not
solved) by RAG, tool use, and reasoning models.

---

## 3. The hardware story (why this is all so expensive)

### GPU

A graphics card. Originally designed for video games, but the same
parallel arithmetic that renders 3D scenes is exactly what neural
networks need. A modern AI workload is 90% matrix multiplications, and
a GPU does thousands of those at once.

### VRAM (video memory)

The fast memory on the GPU. The whole model has to fit in VRAM (or be
swapped in and out, which is slow). This is the binding constraint for
most consumer AI work.

| GPU            | VRAM   | Rough position           |
| -------------- | ------ | ------------------------ |
| RTX 3060       | 12 GB  | Entry consumer           |
| RTX 4090       | 24 GB  | High-end consumer        |
| RTX 5090       | 32 GB  | Top-end consumer (2025+) |
| Apple M3 Ultra | 256 GB | Unified memory, slow-ish |
| NVIDIA A100    | 80 GB  | Datacenter (last gen)    |
| NVIDIA H100    | 80 GB  | Datacenter (current)     |
| NVIDIA H200    | 141 GB | Datacenter (current)     |
| NVIDIA B200    | 192 GB | Datacenter (latest)      |

### Datacenter GPU vs. consumer GPU

Two real differences beyond price:

1. **Memory bandwidth.** A datacenter GPU moves data internally 2–5×
   faster, which matters because LLM inference is memory-bandwidth-
   bound, not compute-bound.
2. **Interconnect.** Datacenter GPUs are wired together with NVLink at
   ~900 GB/s. Consumer GPUs talk to each other over PCIe at ~64 GB/s,
   and across the internet at maybe 0.01 GB/s.

This is why a hyperscaler can run things consumer GPUs can't easily
match — not raw compute, but the *bandwidth between* GPUs.

### TFLOPS

"Trillions of floating-point operations per second." A common GPU
spec. More TFLOPS = more raw math power. Less interesting than memory
bandwidth for inference workloads, but it's the headline number
manufacturers quote.

---

## 4. How inference actually happens

When you type a question into ChatGPT, here's what happens under the
hood. Simplified.

1. **Tokenize.** "Hello, world!" becomes `[15496, 11, 1917, 0]`.
2. **Embed.** Each token is looked up in a giant table to produce a
   vector — say 4,096 numbers per token.
3. **Run through layers.** A 70B model has ~80 transformer layers.
   Each layer takes the vectors in, does a bunch of math (attention +
   feed-forward), produces new vectors of the same shape.
4. **Predict next token.** The output of the final layer is converted
   into a probability distribution over every possible next token.
5. **Sample.** Pick one (using temperature). Append it to the input.
6. **Loop.** Go back to step 3 until you hit a stop token or a length
   limit.

Each step in this loop is one GPU forward pass. Generating 500 tokens
of output = 500 forward passes. That's why output tokens are usually
3–5× more expensive than input tokens.

### Attention

The mechanism inside each layer that lets every token "look at" every
other token in the context. It's the secret sauce of transformers.
Cost grows quadratically with context length, which is why 1M-token
contexts are hard and expensive.

### KV cache

A trick that stores intermediate results so you don't recompute the
whole context for every new token. Typically the largest single chunk
of GPU memory consumed during inference.

---

## 5. The model menagerie

### Dense vs. Mixture-of-Experts (MoE)

- **Dense model:** every parameter activates for every token. Llama
  3.1 70B uses all 70B weights for every token it processes.
- **MoE model:** only a subset of parameters activates per token. The
  model has many "experts," and a router picks a few of them for each
  token. DeepSeek-V3 has 671B total parameters but only 37B active per
  token — roughly the cost of a 37B dense model with the knowledge of
  a 671B one.

MoE is increasingly dominant because it gives you the quality of a
huge model at the inference cost of a smaller one. It's also a key
reason GamerAI's strategy works — see `research/big-models-feasibility.md`.

### Common open-weights models you'll hear about

| Family                | Sizes              | Vibe                                    |
| --------------------- | ------------------ | --------------------------------------- |
| Llama (Meta)          | 1B–405B            | Workhorse, broad. The default.          |
| Mistral / Mixtral     | 7B–8x22B           | Efficient, MoE pioneers in open-weights |
| Qwen (Alibaba)        | 0.5B–110B+         | Strong multilingual, strong code        |
| DeepSeek (V3, R1)     | 671B (37B active)  | Frontier-class MoE. Excellent at math.  |
| Phi (Microsoft)       | 3B–14B             | Small but punchy                        |
| Gemma (Google)        | 2B–27B             | Efficient, well-aligned                 |

### Common closed models

| Provider   | Headliners                          | Vibe                       |
| ---------- | ----------------------------------- | -------------------------- |
| OpenAI     | GPT-4o, o1, o3                      | Broad, mature ecosystem    |
| Anthropic  | Claude (Opus, Sonnet, Haiku)        | Strong code, long context  |
| Google     | Gemini (Pro, Flash, Ultra)          | Multimodal, huge context   |
| xAI        | Grok                                | Real-time, less filtered   |

---

## 6. The stack you'll meet in this codebase

### Ollama

A program that runs LLMs locally with one command:
`ollama pull llama3.2:1b`. Designed to feel as easy as Docker. GamerAI
workers use Ollama as their inference backend.

### llama.cpp

The lower-level library that started it all. Pure C++, runs on almost
anything, supports aggressive quantization. Ollama wraps it.

### vLLM / SGLang / LMDeploy

Production-grade inference servers used by datacenters. Faster than
Ollama for serving many users at once, but heavier to operate. Worth
knowing the names.

### Hugging Face

The GitHub of AI. Where models, datasets, and tokenizers live. If
you've ever seen a URL like `huggingface.co/meta-llama/Llama-3.1-8B`,
that's the model registry.

### LangChain / LlamaIndex

Python libraries that bundle common patterns (RAG pipelines, agents,
tool use). Useful, sometimes overengineered. Many production apps now
skip them and call models directly.

### LangGraph / DSPy / Pydantic AI

Newer-generation frameworks for building agents and structured-output
systems. More opinionated, less magic, increasingly common.

---

## 7. How AI is paid for

You'll see three pricing models:

### Per-token (most APIs)

You pay for input tokens and (more) for output tokens. Example:
- GPT-4o: ~$2.50 per 1M input, $10 per 1M output
- Claude Haiku: ~$0.25 per 1M input, $1.25 per 1M output
- DeepSeek-V3 (third-party hosted): $0.14 per 1M input

### Per-hour GPU rental

You rent the hardware, run whatever model you want. Example:
- RunPod RTX 4090: ~$0.40/hr
- Lambda H100: ~$2.50/hr

### Subscription / flat-rate

ChatGPT Plus, Claude Pro, etc. Convenient for end users; under the
hood, the provider absorbs the per-token cost.

GamerAI's pricing target is **per-token but cheaper** than the closed
APIs, because our marginal compute cost is paid by the gamer's electric
bill, not by us renting H100s.

---

## 8. The big distributed-AI ideas (relevant to this project)

This is the layer that makes GamerAI possible.

### Parallelism — the three flavors

When a model is too big for one GPU, you split it. Three ways:

- **Tensor parallelism (TP):** chop each math operation across GPUs,
  stitch results back together every layer. Needs ultra-fast
  interconnect (NVLink, datacenter only).
- **Pipeline parallelism (PP):** GPU 1 holds layers 1–10, GPU 2 holds
  layers 11–20, etc. Activations flow through like a bucket brigade.
  Tolerates slower networks. **This is what works over the public
  internet** and what GamerAI's big-model plan relies on.
- **Expert parallelism (EP):** for MoE models — each GPU hosts a
  subset of experts; only the relevant ones light up per token.

### Quantization revisited

The key reason you can run a "datacenter model" on a gaming PC at
all. INT4 quantization shrinks Llama 3.1 70B from 140 GB → 35 GB —
fits on two 24 GB GPUs.

### Speculative decoding

A small "draft" model guesses the next several tokens; the big model
verifies them in parallel. Roughly 2–3× faster, no quality loss. A
go-to optimization for serving big models cheaply.

### TEE (Trusted Execution Environment)

A hardware feature that lets a GPU prove it ran the model honestly
and that nobody (not even the machine's owner) could see the data.
Available on H100/H200 today, slowly coming to consumer cards. Key
for the privacy story in our roadmap.

### Federated learning vs. distributed inference

- **Federated learning:** the model is trained across many devices
  without their data ever leaving (think: phones jointly improving a
  keyboard's autocomplete).
- **Distributed inference:** the model is served from many devices
  for any user. **GamerAI is the second one.**

---

## 9. Where AI is going (as of mid-2026)

A short opinionated map.

- **Smaller is the new bigger.** The best 8B models today match
  GPT-4-class quality from 2023. Local inference on phones and laptops
  is becoming real.
- **MoE everywhere.** The frontier (GPT-4, Claude, Gemini, DeepSeek-V3)
  is all MoE. Dense models survive at the small end.
- **Reasoning models eating chat.** o1 / R1-style models with explicit
  thinking phases are setting new state-of-the-art on math, code, and
  agentic tasks.
- **Agents > chatbots.** The flagship use case is no longer "have a
  conversation" but "give the model tools and let it work."
- **Energy is the bottleneck, not chips.** Hyperscalers are running
  out of grid capacity, not silicon. This is exactly the gap GamerAI
  exists to exploit.
- **Open-weights catching up.** The gap between best-closed and
  best-open has narrowed from years to months. A real argument exists
  that open will pass closed this cycle.

---

## 10. Glossary (alphabetical, for reference)

- **Agent** — LLM that can use tools in a loop.
- **Attention** — the mechanism that lets tokens "look at" other tokens.
- **Batch** — running many inference requests together for efficiency.
- **Closed model** — accessible only via API.
- **Completion** — what the model returns.
- **Context window** — how many tokens the model can see at once.
- **Dense model** — every parameter activates for every token.
- **Embedding** — text → vector of numbers capturing meaning.
- **Expert parallelism** — split MoE experts across machines.
- **Fine-tuning** — additional training on top of a base model.
- **Forward pass** — one trip through the model to produce one token.
- **GPU** — graphics card; doubles as AI workhorse.
- **Hallucination** — confidently wrong output.
- **Hugging Face** — the GitHub of AI.
- **Inference** — running a trained model.
- **KV cache** — saved attention state, avoids recomputation.
- **LLM** — Large Language Model.
- **LoRA** — cheap fine-tuning that trains a small add-on.
- **MoE** — Mixture of Experts; sparse model with a router.
- **Multimodal** — handles images/audio/video, not just text.
- **NVLink** — NVIDIA's fast GPU-to-GPU interconnect.
- **Open-weights** — model file is downloadable.
- **Parameter** — one number in the model. "70B" = 70 billion of these.
- **PCIe** — slower bus connecting GPUs to CPUs in consumer machines.
- **Pipeline parallelism** — split layers across machines.
- **Prompt** — the input you give the model.
- **Quantization** — shrinking parameters to fewer bits.
- **RAG** — retrieve documents, stuff into prompt.
- **Reasoning model** — thinks before answering (o1, R1).
- **System prompt** — hidden instruction prepended to every message.
- **Speculative decoding** — small model drafts, big model verifies.
- **TEE** — hardware enclave for confidential compute.
- **Temperature** — randomness knob (0 = deterministic).
- **Tensor parallelism** — split each matmul across machines.
- **TFLOPS** — trillions of math ops per second.
- **Token** — ~¾ of a word; the unit LLMs read and write.
- **Tool use** — agent calling external functions.
- **Training** — making the model in the first place.
- **Transformer** — the neural-network architecture all modern LLMs use.
- **VRAM** — fast memory on the GPU.

---

## Where to go next

- Want to see how this project uses these ideas? Read the main `README.md`.
- Curious about the big-model strategy? `research/big-models-feasibility.md`.
- Want to play with an LLM locally in 5 minutes? Install Ollama, run
  `ollama run llama3.2:1b`, ask it anything.
