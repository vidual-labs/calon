# Contributing to calon

Thanks for your interest in calon. This document covers how to get set up, what the
project expects from a change, and — most importantly — how to tell whether your idea fits.

## Before you start

**calon's scope is deliberately narrow.** It is a booking intake tool: capture a request,
apply scheduling rules, decide, hand off to a calendar, log it. It is explicitly *not* a
CRM, a reservation suite, an automation platform, or a calendar sync engine. See
[`README.md`](README.md#what-calon-is-not) for the full boundary list.

**Please open an issue before starting anything large.** It is much better to agree that a
change fits before you write it than to have a finished pull request declined on scope.
Small fixes — typos, clear bugs, missing tests — need no prior discussion.

The architectural rules that govern the codebase are recorded in [`CLAUDE.md`](CLAUDE.md).
It is written as instructions for AI assistants, but it is the authoritative style and
architecture policy for human contributors too. Read it before your first change.

## Development setup

calon uses [uv](https://docs.astral.sh/uv/) for dependency management and Python 3.12.

```bash
git clone https://github.com/vidual-labs/calon.git
cd calon
make install          # uv sync --all-extras
cp .env.example .env
cp config/calon.example.toml config/calon.toml
```

Useful targets:

| Command | What it does |
| --- | --- |
| `make dev` | Run the development server with reload |
| `make lint` | ruff check |
| `make format` | ruff format |
| `make typecheck` | mypy |
| `make test` | pytest |
| `make check` | lint + typecheck + test — **run this before opening a PR** |

## Branches and commits

Branch names: `feat/…`, `fix/…`, `docs/…`, `chore/…`, `refactor/…`, `test/…`.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(rules): reject bookings that cross the business-hours boundary
fix(ics): fold DESCRIPTION lines at 75 octets
docs(adr): record the decision to defer provider calendar writes
```

Keep commits focused. A commit that changes behavior and reformats 40 unrelated files is
hard to review and harder to revert.

## What a good pull request contains

- **The smallest change that solves the problem.** No speculative abstraction.
- **Tests.** Domain logic gets unit tests; endpoints get integration tests; ICS and
  deeplink output gets golden-file assertions. Cover the awkward cases too — DST
  transitions, window edges, overlapping buffers.
- **Documentation.** `CLAUDE.md` §7 has a table mapping each kind of change to the docs it
  must update. At minimum, any user-visible change needs a `CHANGELOG.md` entry under
  `[Unreleased]`.
- **An ADR** in `docs/adr/` if you made an architectural decision.
- **A justification for any new dependency**, in the PR description. calon is meant to be
  easy to self-host, and every dependency is a small tax on that. A stdlib solution under
  roughly 50 lines beats a new package.
- **`make check` passing.**

## Things that will be declined

- Features that cross a scope boundary in `README.md` (CRM, workflow automation, payments,
  multi-tenancy, AI features)
- Anything that makes an external service, provider account, or lead source *required* for
  the core booking flow — calon is standalone first, and CI enforces it
- Scheduling logic placed in route handlers, templates, or source adapters rather than in
  the pure domain layer
- Renaming or repurposing an already-shipped decision code (add a new one instead)
- Large unrequested refactors bundled with a functional change

None of these are judgments about the idea — several are good ideas for a *different* tool
that talks to calon over its API.

## Reporting bugs and requesting features

Use the issue templates. For bugs, the single most useful thing you can include is the
booking request, the operator rules in effect, and the decision calon returned — that
triple is usually enough to reproduce a scheduling bug exactly.

Security vulnerabilities go through [`SECURITY.md`](SECURITY.md), not the public tracker.

## Code of conduct

Participation is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Licensing of contributions

calon is licensed under the [GNU AGPL-3.0-or-later](LICENSE). By submitting a
contribution, you agree that it is licensed under the same terms. There is no CLA.
