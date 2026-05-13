# GamerAI Community Terms of Service

*Plain-English version. Last updated 2026-05-12.*

GamerAI is a community-powered AI service. People donate spare GPU
time to a shared pool; their friends use that pool to ask questions
of language models. Money is not the point. Honesty is.

These terms apply to every person who interacts with the network:
**contributors** (who run the agent on their machine), **invitees**
(who use the network through a contributor's invite), and **admins**
(who run the coordinator). Accepting an invite, installing the agent,
or operating the coordinator means you agree to these terms.

---

## 1. Honesty, both ways

### As a contributor

- **Run the unmodified agent and an unmodified Ollama.** Do not swap
  in a finetuned model, do not wrap inference calls, do not tamper
  with the output before returning it. We periodically send canary
  prompts with known-good answers to detect modified models;
  contributors who fail canary checks are flagged and may be removed.
- **Advertise capabilities truthfully.** The model name your agent
  reports on `/register` is the model your agent actually serves.
  Don't claim to run a 13B-class model while serving a 1B one.
- **You will see the prompts you serve.** Treat them like you'd treat
  a stranger's diary you stumbled across. Do not log them outside the
  agent's own files, do not share them, do not search them. The agent
  rotates its log file automatically; you can delete it any time.
- **Don't game the network.** Don't run multiple agents on the same
  machine for inflated tier counts. Don't claim work you don't intend
  to serve. Don't intentionally fail jobs.

### As an invitee or paid customer

- **Don't send secrets.** Treat the GamerAI network like a public
  chat room with friends-of-friends listening: whatever you submit
  may be visible to whichever contributor's GPU happens to serve it.
  Do not paste passwords, API keys, PII you wouldn't put on a
  postcard, or anything subject to NDA.
- **Don't abuse the network.** No automated scraping, no
  illegal-content generation, no harassment, no attempts to
  exfiltrate prompts from other users. Standard
  acceptable-use-policy stuff.
- **Use the privacy tier if you need privacy.** When the
  client-side embedding tier ships (Phase 5), use it for any
  prompt where contributor visibility would matter.

### As an admin

- **Do not log full prompts on the coordinator side beyond what's
  needed for debugging.** Truncate or redact in the persisted ledger.
- **Disclose monitoring.** Canary prompts, agreement scoring, and
  worker reputation are part of how the network stays trustworthy.
  They are not surveillance of contributors' machines.
- **Don't read prompts unless you have to.** "Have to" means
  debugging a specific incident, not curiosity.

---

## 2. What we monitor, and why

The coordinator periodically does three things, and you agree to
them by joining:

1. **Canary prompts.** A small number of normal-looking prompts with
   known correct answers are mixed into the queue. Whichever
   contributor claims one of these has their response compared to
   the expected answer. Repeated misses flag the contributor for
   review.
2. **Agreement sampling.** Occasionally the same prompt is served by
   two contributors and the outputs are compared. Contributors whose
   answers consistently diverge from peer consensus are flagged.
3. **Capability auditing.** The coordinator may query the worker's
   Ollama (via the agent) to confirm the advertised model is the one
   actually loaded.

None of these read into prompts on the contributor's machine.
They detect tampering by observing what the network sees.

---

## 3. Service guarantee — none

GamerAI is provided as-is. It will go down. Prompts will sometimes
time out. Contributors will sometimes return junk. We try hard not
to lose data, but the coordinator is a single VPS with a single
SQLite ledger today, and any of: a kernel update, a disk full event,
or an unattended-upgrade reboot mid-job can break things.

If you're using GamerAI for anything where downtime or wrong answers
would cost real money or harm real people, **don't.** Use a paid
service with an SLA. We will eventually offer a paid tier with
better guarantees; until then assume zero.

---

## 4. Earnings, money, and the paid pool

Contributors are **not paid** for serving the free community pool.
Tier, status, and invite slots are the only thing the free pool
rewards.

If and when the paid customer layer ships (Phase 3b.ii in the
roadmap), opt-in GOLD+ contributors will earn a share of paid
revenue for paid jobs they serve. Until that layer ships, **any
"earnings" figure shown in your agent dashboard is a usage
ledger, not an obligation to pay you anything.** It exists so that
when the paid layer is live we can apply it backwards-friendly.

---

## 5. Termination

We can remove a contributor for failing canary checks, abusive
behavior, or material breach of these terms, at our discretion and
without notice. You can stop participating at any time by closing
the agent. The coordinator does not have remote-kill control over
your machine; uninstalling the agent uninstalls our presence on
your hardware.

---

## 6. Privacy

- **Prompts** traverse the contributor network and are visible to
  whichever contributor's machine serves them. The coordinator
  logs job metadata (timestamps, sizes, durations) and stores the
  prompt long enough to deliver the response. We do not sell,
  train on, or otherwise reuse prompt content.
- **Contributor identity** consists of an email address (for invite
  chain) and a bearer token hash. We do not collect Windows
  hostnames, MAC addresses, or hardware fingerprints beyond the
  capabilities the agent voluntarily reports.
- **Logs** rotate. Stored prompts are eligible for purge after 30
  days. If you want yours purged sooner, email the admin.

---

## 7. Jurisdiction

If a dispute arises, both parties agree to discuss it in good
faith before involving lawyers. If lawyers must be involved, the
jurisdiction is the state of residence of the GamerAI admin
operating the coordinator.

---

## 8. Changes

These terms can change. When they do, the coordinator's
`/tos` endpoint will publish the new version with an updated date.
A contributor whose agent has been running across a terms change
will see a notice on their next agent log line and must re-accept
in the host admin UI to continue serving. An invitee or paid
customer will be prompted at next login.

---

## TL;DR

Be honest. Run what you say you're running. Don't read other
people's prompts. Don't put secrets into the network. If you
break trust, you're out. If we break trust, leave. We are doing
this for the community, not for shareholders.
