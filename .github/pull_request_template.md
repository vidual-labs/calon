## What this changes

<!-- One or two sentences on the effect of this change, from an operator's point of view. -->

## Why

<!-- The problem being solved, or a link to the issue. -->

Closes #

## How

<!-- Notable implementation decisions, and anything a reviewer should look at closely. -->

## Checklist

- [ ] The change is in scope (see `README.md` — calon is not a CRM, reservation suite,
      automation platform, or calendar sync engine)
- [ ] calon still works with **zero** external sources configured (standalone first)
- [ ] Scheduling logic lives in `src/calon/domain/`, not in routes, templates, or adapters
- [ ] Tests added or updated (unit for domain logic, integration for endpoints)
- [ ] `make check` passes locally
- [ ] `CHANGELOG.md` `[Unreleased]` updated for any user-visible change
- [ ] Docs updated per the table in `CLAUDE.md` §7
- [ ] An ADR added to `docs/adr/` if this makes an architectural decision
- [ ] No new runtime dependency, **or** it is justified below

## New dependencies

<!-- Delete this section if none. Otherwise: which package, why, and what it replaces. -->

_None._
