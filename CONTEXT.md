# Hivemind

Hivemind is a shared memory service for an organization's AI agents: one organization runs one Hivemind, and all of its agents read and write a single pool of entries, so work done with one agent is discoverable work for every other agent in the organization.

This file is the canonical glossary for the project. It is a vocabulary, not a spec — for behavior, see `SPEC.md`; for decisions and their reasons, see `docs/adr/`.

## Language

### Core concepts

**Hivemind**:
The shared memory service for one organization. One organization, one pool.
_Avoid_: backend, memory database

**Entry**:
One unit of memory: a distilled observation, analysis, or decision written by an agent and readable by every agent in the organization.
_Avoid_: memory, record, note, post

**Kind**:
The category of an entry: `fact`, `insight`, or `decision`.
_Avoid_: type, category (the API field is `kind`)

**Fact**:
A dated, checkable observation about the world (e.g., "the churn model uses weekly cohorts").
_Avoid_: claim, datum

**Insight**:
A long-form analysis or finding: a short summary plus a long body, optionally referencing artifacts.
_Avoid_: report, deep-dive (in prose)

**Decision**:
A choice the organization (or a part of it) made, with its rationale.
_Avoid_: ruling, resolution

### Provenance

**Author**:
The human who owns an entry, determined by the credential used to write it. The author is accountable for the entries under their key.
_Avoid_: owner, creator, user

**Agent**:
The agent instance (framework + instance identifier) that performed a read or write. Recorded on every entry for provenance.
_Avoid_: bot, worker, client

**Provenance**:
The origin trail of an entry: author, agent, occurrence time, creation time, and source references.
_Avoid_: audit, lineage

### Lifecycle

**Supersession**:
The explicit replacement of one entry by a newer one; the superseded entry is retained and hidden by default. Supersession is a claim, not an arbitration — the reading agent decides.
_Avoid_: overwrite, update, invalidate (say "supersede"; never "update an entry" — entries are immutable)

**Withdrawal**:
Marking an entry no longer valid without replacing it (retraction, or admin correction). Withdrawn entries are retained, never deleted.
_Avoid_: delete, purge

**Quality**:
The bounded re-scoring signal derived from feedback on an entry; it affects retrieval, not governance.
_Avoid_: score (in specs, "score" always means the retrieval score; "quality" is the feedback-derived factor)

**Feedback**:
A reporting agent's verdict that an entry it relied on was `helpful`, `stale`, or `wrong`. Aggregated into an entry's quality.
_Avoid_: rating, review

### Time

**Occurrence time**:
When the observation behind an entry was actually made; may predate creation (backdating allowed). The basis of the "memory date" filter.
_Avoid_: timestamp (always say occurrence time or creation time explicitly)

**Creation time**:
When the entry was written into the pool (server-assigned, immutable).
_Avoid_: timestamp

### Session control

**Kill switch**:
Disabling an agent's Hivemind integration for a session (e.g., during non-analyst work). A client-side act: the server has no session concept and is unaware of off sessions.
_Avoid_: pause, session disable, opt-out

**Scope**:
The intended audience of an entry. In v1 every entry is org-wide; the field exists as a seam for future narrowing (team, project, channel).
_Avoid_: namespace, channel (those are the future concepts, not the v1 value)