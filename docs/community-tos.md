# GamerAI Community Terms of Service

*Plain-English version. Last updated 2026-08-22.*

GamerAI is a community-driven AI service. People donate spare GPU
time to a shared pool; their invitees and (in time) paid customers
use that pool to ask questions of language models. We think AI
should be something users can be proud to use and run on their own
hardware, not just rented from a handful of hyperscalers — so we're
building it that way.

These terms apply to every person who interacts with the network:
**contributors** (who run the agent on their machine), **invitees**
(who use the network through a contributor's invite), and **admins**
(who operate the coordinator). Accepting an invite, installing the
agent, or operating the coordinator means you agree to these terms.

---

## 1. Honesty, both ways

### As a contributor

- **Run the unmodified agent and an unmodified inference engine.**
  Do not swap in a finetuned model, do not wrap inference calls,
  do not tamper with the output before returning it. We
  periodically send canary prompts with known-good answers to
  detect modified models; contributors who fail canary checks are
  flagged and may be removed.
- **Advertise capabilities truthfully.** The model your agent
  reports on registration is the model your agent actually serves.
  Don't claim to run a 13B-class model while serving a 1B one.
- **You will see the prompts you serve.** Treat them like you'd
  treat a stranger's diary you stumbled across. Do not log them
  outside the agent's own files, do not share them, do not search
  them. The agent rotates its log file automatically; you can
  delete it any time.
- **Do not enable inference-engine debug logging.** Specifically,
  do not run Ollama with `OLLAMA_DEBUG=1` (or any equivalent
  verbose flag) while your agent is serving the community queue.
  The agent itself does not log prompt text, but debug mode does —
  and that is the most plausible way prompts could leak from your
  machine. The agent spawns its inference engine with debug off by
  default; a contributor who launches it themselves controls its
  environment.
- **Don't game the network.** Don't run multiple agents on the
  same machine for inflated tier counts. Don't claim work you
  don't intend to serve. Don't intentionally fail jobs.

### As an invitee or paid customer

- **Don't send secrets.** Treat the GamerAI network like a public
  chat room with friends-of-friends listening: whatever you submit
  may be visible to whichever contributor's GPU happens to serve
  it. Do not paste passwords, API keys, PII you wouldn't put on a
  postcard, or anything subject to NDA.
- **Don't abuse the network.** No automated scraping, no
  illegal-content generation, no harassment, no attempts to
  exfiltrate prompts from other users. Standard
  acceptable-use-policy stuff.
- **Use higher-confidentiality options when they're available.**
  We have plans for routing paths that don't require the
  contributor to see plaintext; until those ship, contributor
  visibility is what you get.

### As an admin

- **Do not log full prompts beyond what's needed for debugging.**
  Truncate or redact in persisted records.
- **Disclose monitoring.** Canary prompts, agreement scoring, and
  worker reputation are part of how the network stays trustworthy.
  They are not surveillance of contributors' machines.
- **Don't read prompts unless you have to.** "Have to" means
  debugging a specific incident, not curiosity.

---

## 2. What we monitor, and why

The coordinator periodically does three things, and you agree to
them by joining:

1. **Canary prompts.** A small number of normal-looking prompts
   with known correct answers are mixed into the queue. Whichever
   contributor claims one of these has their response compared to
   the expected answer. Repeated misses flag the contributor for
   review.
2. **Agreement sampling.** Occasionally the same prompt is served
   by two contributors and the outputs are compared. Contributors
   whose answers consistently diverge from peer consensus are
   flagged.
3. **Capability auditing.** The coordinator may query the worker's
   inference engine (via the agent) to confirm the advertised
   model is the one actually loaded.

None of these read into prompts on the contributor's machine.
They detect tampering by observing what the network sees.

---

## 3. Service guarantee

GamerAI is provided on a best-effort basis. The service may go
down, prompts may time out, contributors may sometimes return
poor-quality output. We work to minimize all of those, but we
make no guarantees about uptime, durability, or correctness.
Infrastructure will evolve over time, and reliability may change
as it does.

If you're using GamerAI for anything where downtime or wrong
answers would cost real money or harm real people, **don't.** Use
a paid service with a contractual SLA. Paid-tier offerings with
stronger guarantees may follow; until then, plan for zero.

---

## 4. Earnings, money, and the paid pool

Today, contributors are **not paid** for serving the free
community pool. Tier, status, and invite slots are what the free
pool rewards.

We have plans for an optional paid customer layer where opt-in
contributors will earn a share of paid revenue for paid jobs they
serve. The exact structure of that is still being designed; any
"earnings" figure shown in your agent dashboard today is a usage
ledger, not a contractual obligation to pay you anything. It
exists so that when paid features ship we can apply them in a
backwards-friendly way.

We intend to build GamerAI into a sustainable business. The free
community pool is the foundation, with paid options layered on
top. Both sides will evolve over time.

---

## 5. Termination

We can remove a contributor for failing canary checks, abusive
behavior, or material breach of these terms, at our discretion
and without notice. You can stop participating at any time by
closing the agent. The coordinator does not have remote-kill
control over your machine; uninstalling the agent uninstalls our
presence on your hardware.

---

## 6. Privacy

- **Prompts** traverse the contributor network and are visible to
  whichever contributor's machine serves them. We log job
  metadata (timestamps, sizes, durations) and retain prompt
  content long enough to deliver responses and maintain your
  history. We do not sell, train on, or otherwise reuse prompt
  content.
- **Contributor identity** consists of an email address (for
  invite chain) and a bearer token hash. We do not collect
  hostnames, MAC addresses, or hardware fingerprints beyond the
  capabilities the agent voluntarily reports — which includes your
  GPU's model name and VRAM amount (e.g. "RTX 4090, 24 GB"), shown
  on your account's Machines page and to admins operating the
  network. That's a shared model descriptor, the same string
  millions of other owners of that card would report, not a unique
  identifier — it's used to label your machine in the dashboard and
  is not cross-referenced against anything to re-identify you.
- **Logs** rotate. Stored prompts are eligible for purge after a
  reasonable retention window. If you want yours purged sooner,
  email the admin.

---

## 7. Disputes

If a dispute arises, both parties agree to:

1. **Talk first.** Email the admin and try to resolve the issue
   in good faith. Most things end here.
2. **Then arbitration.** If direct discussion doesn't resolve it,
   binding individual arbitration through a recognized arbitration
   service (such as the AAA or JAMS), with each party bearing
   their own costs except as determined by the arbitrator. Class
   arbitration is not permitted; disputes are individual.
3. **Court only as a last resort.** If arbitration is unavailable
   or unenforceable for a specific matter, jurisdiction is the
   state of residence of the GamerAI operator.

This is not a substitute for legal advice. If you have a lawyer,
listen to your lawyer over us.

---

## 8. Changes

These terms can change. When they do, the coordinator's `/tos`
endpoint will publish the new version with an updated date.
Existing members will see a notice on next agent log line (for
contributors) or next sign-in (for invitees and paid customers)
and may be asked to re-accept before continuing.

---

## TL;DR

Be honest. Run what you say you're running. Don't read other
people's prompts. Don't put secrets into the network. If you
break trust, you're out. If we break trust, leave.

GamerAI is community-driven — AI infrastructure you can be proud
to use and run on your own hardware, not just rented from
hyperscalers. The free contributor pool is the foundation; we
plan to layer paid options on top and grow this into a real
business while keeping the community-first part intact.
