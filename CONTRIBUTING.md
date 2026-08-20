# Contributing to GamerAI

Thanks for taking a look. Honest status first, so you know what you're
walking into.

## Where this project is right now

GamerAI is a working, publicly-deployed system with one operator and
one production coordinator (`ai.dallinlayton.com`). It's source-available
(see [Licensing](#licensing) below — not a traditional open-source license)
so you can read it, run it, fork it for noncommercial purposes, and see how
a real distributed job-queue + membership system is put together — not
because it's staffed to review a high volume of external pull requests.
Issues and small, focused PRs (bug fixes, doc corrections, test additions)
are genuinely welcome; large feature PRs are more likely to sit unless we
talk first.

## Licensing

This project is licensed under [PolyForm Noncommercial
1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0), not a
standard OSI open-source license — noncommercial use (reading, running,
modifying, forking, learning from it) is unrestricted, but commercial use
requires a separate agreement with the copyright holder. **By submitting a
pull request, you agree that your contribution is licensed under the same
terms as the rest of the project, and you grant the maintainer the right to
relicense the project (including your contribution) under different terms
in the future** — e.g. if a commercial license gets negotiated with a
company. If that's a dealbreaker for a contribution you have in mind, open
an issue first and say so before sending a PR.

## Before opening a PR

- **For anything nontrivial, open an issue first.** Describe the
  problem and your proposed approach. This saves both of us from a
  large diff landing against a direction the project isn't taking.
- **Check `docs/project-gaps.md`.** It's a live, dated list of known
  gaps by severity — a good place to find something worth working on,
  and a good way to check whether what you're about to build is
  already planned differently than you'd guess.
- **Security issues do not go in a public issue.** See
  [`SECURITY.md`](SECURITY.md).

## Working in this repo

- `docker compose up --build` gets a full local stack running — see
  README §9 for the walkthrough, including a mock-inference mode that
  needs no GPU or Ollama install.
- Run the test suite with `python -m unittest discover tests` before
  opening a PR. CI runs the same suite plus a `docker-compose` config
  lint and a shellcheck pass on the infra scripts — all three need to
  be green.
- Match the style already in the file you're touching before
  introducing a new pattern. This codebase leans heavily on inline
  comments that explain *why* a non-obvious decision was made — that's
  deliberate; keep doing it in code you add.
- Small, focused commits with clear messages. Squash noisy WIP history
  before opening the PR.

## What's especially useful right now

- Tests for existing untested edge cases (see `docs/project-gaps.md`
  §4 "Engineering hygiene" for known gaps).
- Bug reports with reproduction steps, especially anything found by
  actually running the agent on real hardware — the founder's test
  fleet is small.
- Documentation fixes. The docs are extensive but this is still a fast-
  moving one-person project; staleness creeps in.

## What's unlikely to be merged without prior discussion

- New paid-tier / billing surface (Phase 3b.ii in the roadmap — not
  started, and the economics need to be right before code exists).
- Anything that changes the membership/trust model without matching
  changes to `docs/community-tos.md`.
- Large refactors with no attached issue explaining the motivation.

## Code of conduct

Be direct, be kind, assume good faith. Disagreements about approach
are normal; personal attacks aren't tolerated in issues, PRs, or
review comments.
