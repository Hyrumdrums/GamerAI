# GamerAI — one-liner + blurb

For a resume link / LinkedIn post / portfolio card. Repo:
https://github.com/Hyrumdrums/GamerAI — Live: https://ai.dallinlayton.com
— Try it with no install: https://ai.dallinlayton.com/demo

## One-liner (bio / resume line)

Built GamerAI, a community-powered AI network — contributors lend idle
GPU time from their gaming PCs to a shared job queue and earn tier-based
access in return, including a mode that pools two machines' VRAM over
the network to run a bigger model than either card can hold alone.

## Short blurb (LinkedIn post / portfolio card, ~3 sentences)

Centralized AI inference is expensive because it's scarce, not because
it's hard to run — millions of gaming GPUs sit idle every day that could
serve a friend group's everyday AI needs instead. I built GamerAI to
test that: a FastAPI coordinator does job routing, quota enforcement, and
per-member auth over a Redis queue, while a self-updating Windows agent
lets anyone's gaming PC join the shared pool and earn tier-based access
for themselves and the people they invite — no subscription, no invite
gate to get started. The part I'd point another engineer at first is
[smart mode](https://github.com/Hyrumdrums/GamerAI/blob/main/docs/smart-mode.md):
splitting a 14B-class model's layers across two separate contributor
machines via llama.cpp RPC so their combined VRAM runs a model neither
card could hold alone. Try it with no install at
https://ai.dallinlayton.com/demo, or read the code (and the honest,
dated list of what's still rough) at
https://github.com/Hyrumdrums/GamerAI.

## Notes

- Swap "Try it..." wording once the repo is actually public — right now
  the GitHub link 404s for anyone outside the repo's collaborators.
- The blurb leans on `docs/smart-mode.md` and `docs/project-gaps.md` as
  the two things worth a reviewer's five minutes; keep both current if
  this blurb gets reused later, since a stale link undercuts the pitch.
