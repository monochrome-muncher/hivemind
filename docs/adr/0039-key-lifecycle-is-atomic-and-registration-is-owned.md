# Key lifecycle is atomic, keys are unique by schema, and a registration is owned by its alias

Refines ADR 0012 (step 2, idempotent re-registration; the "legacy rows
keep working" clause), ADR 0028 and ADR 0031. Supersedes only those two
clauses of ADR 0012.

## Context

A review of the access plane found that the invariants ADRs 0028 and
0031 state were conventions, not guarantees:

* `activate` flipped the agent to `active` and only then inserted the
  key; a `revoke` landing in between left a **revoked agent with a live
  key** at full trust, because `verify` never looked at the agent's
  status.
* "One key per agent" and "one live org key" were not in the schema. A
  CLI `issue-agent` on a pending agent plus a REST activate produced two
  live keys; concurrent org-key rotations produced several. The CLI also
  silently restored a revoked agent's stale trust level and fleet.
* Re-registering a pending name was an idempotent no-op whatever the
  alias, so the first registrant owned `owner_alias` and the real owner's
  later registration got a success that was silently ignored: the admin
  delivers the key to the alias on the record. A name that was already
  active or revoked answered a bare `name_conflict`, which an agent that
  simply forgot it had registered could not tell from "someone else's".
* Pre-v2 `user` keys (and name-less `agent` keys) still resolved to a
  credential that reads every entry. ADR 0012 let them "keep working
  until migrated"; no migration ever existed.

## Decision

1. **An agent key authenticates only while its agent is `active`.**
   `verify` resolves the key and the agent in one joined query; a key
   whose agent is pending, revoked or missing is no credential (`None`,
   a 401), not a level-0 one. The status is the single authority; a
   leftover key row is inert.
2. **Transitions are guarded and serialised.** Activation and revocation
   remain status-guarded `UPDATE`s. Issuing a key runs under the agent
   row's lock and requires the agent to still be `active` (otherwise
   `InvalidAgentStatus`); deleting the key runs under the same lock and
   only while the agent is still `revoked`. Revoke flips the status
   first. Any interleaving therefore ends in a state equal to some serial
   order, and never a revoked agent with a key.
3. **Uniqueness is in the schema** (migration `0007`): a unique partial
   index on `credentials(agent_name) WHERE kind = 'agent'` and one on
   `credentials((kind)) WHERE kind = 'org'`. Org-key rotation (API and
   CLI) takes an advisory transaction lock so concurrent rotations
   serialise instead of failing. The migration first resolves existing
   duplicates deterministically: per agent name, and for the org key, the
   **newest** row (`created_at`, ties by `key_hash`) survives and older
   ones are deleted, i.e. their keys are revoked. The deletion is not
   restored by the rollback.
4. **The CLI follows the REST activate path.** `hivemind-keys issue-agent`
   issues a key for an `active` agent that has none. For a `pending` or
   `revoked` agent it refuses unless `--trust-level` and `--home-fleet`
   are given, and then activates with exactly those values, in one
   transaction with the key. It never restores stale trust or fleet.
5. **A registration is owned by its alias.** The first registrant's
   `owner_alias` is final (no back-fill). Registering an existing name:
   * **same alias** (trimmed, case-insensitive; a missing alias equals
     only a missing alias) → success, `already_registered`, and the
     current status with what to do next: pending ("do not register
     again"), active ("ask your admin for the agent key"), revoked
     ("rejected; ask your admin or pick another name"). REST answers 200
     (201 for a new name); MCP returns the same fields.
   * **any other alias** → `name_conflict` (REST 409) with one fixed
     text for every status, revealing nothing about the other agent.
6. **Retired key kinds are dead.** `verify` returns `None` for `kind =
   'user'` and for `agent` rows without a name. This supersedes the
   "legacy rows keep working" clause of ADR 0012. Operators should
   delete such rows; the schema CHECK still allows the kind (narrowing it
   would break an older sibling pod's expand-and-contract window).

## Consequences

* **Rolling deploy (ADR 0020 window).** Old pods do not know the new
  guards. While both run: concurrent org-key rotations on an old pod hit
  the new unique index (measured: about 3 of 4 racers get a 500; serial
  rotations are fine); an old pod's REST activate on an agent that already
  holds a key row 500s every time (the agent is left active with the old
  key); and new pods answer 401 for legacy, pending, revoked and
  no-record keys that old pods still accept, so such clients flap until
  the rollout finishes. All of it is admin-only, human-rate or limited to
  the rollout window. Migration `0007` builds its indexes without
  `CONCURRENTLY` (ADR 0020 prefers it): the table holds a handful of rows,
  and building them in the dedupe's transaction keeps dedupe-then-enforce
  atomic.
* **Dead credential rows.** Legacy `user` rows, name-less agent rows and
  keys of missing / pending / revoked agents are inert but remain in the
  table and would authenticate again after a rollback to an older
  release. `hivemind-keys list` marks them `[dead]`, `migrate` logs a
  warning with their count, and the runbook gives the SQL to delete them.
* `issue-agent` rejects `--trust-level` / `--home-fleet` for an already
  active agent (changing those is the admin PATCH) and validates the
  fleet id, instead of ignoring or crashing on them.

* One query per agent request instead of two.
* Existing pools lose any pre-v2 key on deploy (there is no migration
  path for them by design), and any duplicate agent or org key older
  than the newest.
* A squatter on a pending name now simply gets `name_conflict`; the
  legitimate owner must pick another name and the admin may reject the
  squatted record (`revoke` from `pending`; the name stays reserved,
  ADR 0028). The alias remains unverified: the admin should still
  confirm the requester out of band. Registration is still not audited.
* An agent that registered with an alias and later re-registers without
  it is told the name is taken; the plugin skill must keep the alias
  it registered with (wording owned by the plugin branch).
