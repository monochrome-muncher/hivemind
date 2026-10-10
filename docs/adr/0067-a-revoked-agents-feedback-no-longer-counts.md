# A revoked agent's feedback no longer counts

## Context

Revoking an agent (ADRs 0012, 0028) deletes its key and sets it `revoked`,
but its feedback rows stay. They keep shaping the `quality` multiplier
(ADR 0051), keep entries in the `flagged` list (ADR 0054) and keep showing
in an entry's recent reports, and nobody can remove them: a revoked agent
can no longer call `hive_feedback`, and an admin has no verb for it. A
report from an agent the operator decided not to trust is the kind that
should stop counting.

## Decision

1. **Left out of every read.** A feedback row whose reporter (`"user"`) is
   the name of a `revoked` agent is left out of `feedback_counts`,
   `quality_counts`, `list_feedback`, `list_feedback_many` and the
   `flagged` filter, on both adapters (covered by the Store contract
   suite). `PgStore` adds `NOT EXISTS (SELECT 1 FROM agents a WHERE
   a.name = f."user" AND a.status = 'revoked')` to those queries;
   `agents.name` is unique, so it is an index probe, and no migration is
   needed. `MemoryStore` applies the same rule from its agents.

2. **Nothing is deleted.** The rows stay. Re-activating the agent (allowed
   from `revoked`, ADR 0028) brings its feedback back, so the rule is
   reversible and revocation needs no extra write.

3. **Only `revoked`.** Pending and demoted (`untrusted`, dormant) agents
   keep counting. Rows whose reporter matches no agent (the `admin` key,
   dev-mode users) keep counting.

4. **Exact name match.** `"user"` is compared with `agents.name` as
   stored, like the credential join in `auth.py`. Names are unique ignoring
   case (ADR 0045), and an agent's `"user"` is its registered name, so no
   case folding is needed.

## Consequences

- Revoking an agent can change the quality and `flagged` state of entries
  it reported on, with no write to those entries.
- A re-activated agent's old reports count again, including ones it made
  before it was revoked.
- A revoked agent's reports are no longer readable through `hive_get` or
  the REST read either; the rows are only visible in the database.
- Every feedback read does one extra probe on `agents` per row.
