# Security Policy

GamerAI is a live, publicly-deployed system (`ai.dallinlayton.com`)
with real members, real contributor machines, and a real trust model
described in `docs/community-tos.md`. If you find a security issue,
please report it privately rather than opening a public issue — a
public report on a live system gives an attacker a head start.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository:

**[Security → Report a vulnerability](../../security/advisories/new)**

This opens a private draft advisory visible only to you and the
maintainer — nothing is public until a fix ships and the maintainer
chooses to disclose. GitHub's private reporting flow requires no
account beyond a GitHub login.

If that link 404s for you, the feature hasn't been turned on for this
repo yet (Settings → Security → "Private vulnerability reporting") —
please open a regular issue titled generically (no exploit details)
asking the maintainer to enable it, and hold the details until it's on.

## What's in scope

- The coordinator API (`coordinator/`) — auth, job routing, quota
  enforcement, the membership/invite/signup system.
- The web client (`client/`) — session handling, XSS, the public
  demo/signup/invite surfaces.
- The Windows agent (`windows-agent/`) — the self-update signing
  scheme, pairing/signup flows, local credential storage.
- Deployment/infra (`infra/`) — anything that would let an attacker
  pivot from a compromised contributor machine or a compromised VPS
  into the source-of-truth GitHub repo (see `docs/project-gaps.md`'s
  notes on the deploy-key blast radius for the kind of thing that
  matters here).

## What's already known

Check `docs/project-gaps.md` first — it's a maintained, dated,
severity-tagged list of known gaps, including several accepted-risk
items with the reasoning written down. A report that duplicates a
documented, already-accepted-risk item is still welcome, but it'll
save you time to check first.

## Response

This is a one-person project. Best-effort acknowledgment within a few
days; timeline to a fix depends on severity. Critical issues affecting
the live production coordinator get priority over anything else on the
board.
