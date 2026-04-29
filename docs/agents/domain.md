# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout

This is a single-context repo.

Read these files before substantial planning, diagnosis, architecture work, or TDD implementation:

- `CONTEXT.md` at the repo root, if present.
- `docs/adr/`, if present, for architectural decisions that touch the area being changed.
- `docs/REQUIREMENTS.md` for intended product requirements.
- `docs/SPECIFICATION.md` for the current implemented behavior.

If `CONTEXT.md` or `docs/adr/` do not exist, proceed silently. Do not flag their absence unless the task is specifically about documenting domain context or architectural decisions.

## Use Domain Vocabulary

When output names a domain concept in an issue title, refactor proposal, hypothesis, test name, or implementation plan, use the terms from `CONTEXT.md` when it exists. If a concept is not documented there, prefer the terms already used in `docs/REQUIREMENTS.md`, `docs/SPECIFICATION.md`, and the codebase.

## Flag ADR Conflicts

If a recommendation or implementation contradicts an existing ADR, surface it explicitly rather than silently overriding it.
