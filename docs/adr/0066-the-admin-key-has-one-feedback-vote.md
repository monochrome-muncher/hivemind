# The admin key has one feedback vote per entry

> Amends [ADR 0060](0060-the-admin-key-writes-self-or-org-entries.md)
> (the admin key's identity: it reports feedback as `admin`).

## Context

Feedback is one row per (entry, user, agent) (SPEC §4.2). An agent key's
agent identity is its registered name, so one agent has one vote on an
entry. The admin key has no agent identity, so `record_feedback` used the
self-reported `agent` field: every distinct label was a new counted row,
all shown to readers with reporter `admin`. Ten labels were ten votes, and
the quality multiplier (SPEC §6.4) counts rows.

## Decision

1. **The admin key reports as `admin`.** When the credential is the admin
   key, the feedback row's agent is always `admin`, the reserved name
   ADR 0060 uses for the author of an admin-written entry. A self-reported
   `agent` is ignored, as an agent key's credential agent wins over it.
   The admin key has one row, so one vote, per entry; a later verdict
   replaces it.

2. **No `agent` needed.** The admin key does not have to pass `agent`: it
   gets neither `agent_identity_required` (REST) nor `agent_unresolved`
   (MCP) for leaving it out. The rule lives in one helper,
   `reporter_agent(credential, agent)` in `services/feedback.py`, which the
   service and the MCP pre-check both call.

3. **Others unchanged.** Agent keys keep their credential agent. A
   credential that is neither (dev mode, not access-controlled) still
   self-reports `agent`, and is refused without one.

## Consequences

- An admin's feedback no longer inflates counts or the quality multiplier.
- Rows the admin key wrote earlier under other labels stay as they are
  (no migration); they still count, one row per label, and are not
  rewritten or merged.
- The admin key can give feedback with only an entry id and a verdict.
