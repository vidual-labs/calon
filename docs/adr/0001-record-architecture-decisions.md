# 1. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

calon is starting from an empty repository. The decisions made in the first weeks — the
storage engine, the calendar strategy, the boundary against external lead sources — are the
ones most likely to be quietly reversed later by someone who does not know why they were
made, including future maintainers and AI assistants working from a partial view of the
codebase.

Commit messages do not survive as explanation, and a design document that is edited in place
loses the reasoning as soon as the design changes.

## Decision

We will record architecturally significant decisions as Architecture Decision Records,
following Michael Nygard's format, in `docs/adr/`.

- One decision per file, named `NNNN-short-kebab-title.md`, numbered sequentially.
- Each has **Status**, **Date**, **Context**, **Decision**, and **Consequences**.
- ADRs are **immutable once merged.** To reverse one, write a new ADR that supersedes it and
  mark the old one `Superseded by NNNN`.

A decision is "architecturally significant" if reversing it later would be expensive: adding
or removing a dependency of consequence, changing the storage engine, altering a public
contract, or moving a boundary between components.

## Consequences

- The reasoning behind a decision outlives the person who made it, and outlives any single
  conversation.
- Reviewers can challenge the reasoning rather than only the code.
- `CLAUDE.md` can point at this directory instead of restating rationale, which keeps the
  policy file short.
- Small cost: any pull request that makes an architectural decision carries one more file.
