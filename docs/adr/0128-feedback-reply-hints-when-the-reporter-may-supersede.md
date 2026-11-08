# The feedback reply says when the reporter may supersede the entry

Builds on [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) (who may supersede), [ADR 0053](0053-feedback-on-several-entries-at-once.md) (batch feedback) and [ADR 0060](0060-the-admin-key-writes-self-or-org-entries.md) (the admin key's writes).

Status: Accepted.

## Context

An agent that reports `stale` or `wrong` with `hive_feedback` leaves a note, and the old entry stays active. Nothing told the agent that it could often write the correction itself as a successor. Flags on fleet entries sat as notes until an admin acted. The production flag backlog is 0 today, so this is a safeguard, not a fix for a pile.

## Decision

1. The feedback service works out, per entry, whether the reporter could supersede it with a write at its default scope: `may_supersede(entry, new_scope, new_fleet_id, writer)` with the scope and fleet `resolve_write_scope` gives when no scope is passed. It is computed in `FeedbackService` from the entries already loaded, so there is no new store call, and carried as `FeedbackOutcome.may_supersede`.
2. It is true only for `stale` or `wrong`, never `helpful`; never for the admin key (its writes are human-driven and org-only, ADR 0060) nor a legacy (non-access-controlled) credential, whose default scope is `org` and would widen the entry; false when the caller cannot write. Only access-controlled agent keys get it.
3. REST returns the boolean `may_supersede` on each feedback result. MCP adds `fix` only when it is true: a fixed sentence naming the entry id, `supersedes` and "no scope"; a batch reply carries `fix` once with the ids the caller may supersede. It is left out otherwise, as MCP replies are compact.
4. The text is fixed server text; the only entry-derived part is the id. Entries stay data (ADR 0043).

## Consequences

- No always-on text changes: the tool descriptions, server instructions and skill are untouched (their budget is nearly full), and the reply explains itself where it is read.
- A caller that sees the hint may still be refused by the write (the entry may have been superseded meanwhile); the hint is advice, not a grant.
- Other REST clients see one new boolean, default `false`.
