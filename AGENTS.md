# AGENTS.md — Hivemind

Hivemind is a shared memory service for an organization's AI agents (one Postgres-backed pool all agents read and write). This repo is **spec-first**: it currently contains docs only, no code yet.

## Before doing anything here

Read in this order:

1. [README.md](README.md) — what Hivemind is (short)
2. [SPEC.md](SPEC.md) — the v1 spec; the source of truth for scope and behavior
3. [CONTEXT.md](CONTEXT.md) — the glossary; the source of truth for **terminology**
4. [docs/adr/](docs/adr/) — decisions and their reasons; read the relevant ADR before touching the area it governs

## Rules

- **Terminology is the glossary.** Use the canonical terms from CONTEXT.md. Say "supersede", never "update" or "overwrite" an entry (entries are immutable — ADR 0001). "Memory date" means `occurred_at`, not `created_at`.
- **Implement what the spec commits to.** v1 scope is SPEC.md §1–§8. The non-goals (§9) are non-goals: no UI, no multi-tenancy, no knowledge graph, no passive capture, no Redis, no sharding. Build the §10 extensions only when their stated trigger fires.
- **Decisions go in ADRs.** A new permanent design choice (data model, API shape, deployment, credentials) gets a new `docs/adr/NNNN-slug.md` (next sequential number) and a matching SPEC.md section. A decision that contradicts an existing ADR is resolved by writing a new ADR that supersedes it — never by quietly editing the old one.
- **Glossary stays current.** When a term is resolved (during design, review, or implementation), update CONTEXT.md in the same change. CONTEXT.md is a glossary: no implementation details, no prose, no rationale (that's the ADR's job).
- **One source of truth per meaning.** The spec is the single home for behavior; the ADRs for reasons; the glossary for words. Don't restate spec behavior in the README or docs — link to it.

## Conventions for the codebase (when it exists)

- This file gains a **Commands** section (build, test, run, lint) as soon as the first code lands; until then there is no build system.
- Follow the SPEC.md API surface exactly: REST endpoints §5.1, MCP tools §5.2, retrieval pipeline §6. Config knobs (RRF weights, decay half-life, quality formula, embedding model/dimension) live in deployment config, not in code constants.

## State of this repo

Docs only: `README.md`, `SPEC.md`, `CONTEXT.md`, `docs/adr/0001`–`0008`. No code, no tests, no CI yet.