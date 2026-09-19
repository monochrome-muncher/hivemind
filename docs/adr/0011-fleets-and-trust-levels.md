# Fleets and trust levels: home-fleet scoping with a four-level privilege ladder

ADR 0002 committed Hivemind to a *flat org-wide pool*: one pool, `scope` a
single-value `org` tag, every agent reading and writing everything. That
commitment made per-agent privacy impossible, and SPEC §9 listed "trust
tiers" as an explicit non-goal — a deliberate divergence from **Caura**,
the fleet-memory project Hivemind borrows its MCP-native shape from
(SPEC §3). **Decision: re-adopt the Caura tier + fleet idea.** The pool
is partitioned by **fleets**, and every agent carries a **trust level**
that gates what it may read and write.

This ADR supersedes ADR 0002 (flat org-wide pool). ADR 0002's *seam*
(the `scope` field) survives and is finally used: it now carries real
values. The SPEC §9 non-goal line "no trust tiers" and the SPEC §3
divergence note ("no trust tiers" vs. Caura) are struck/corrected by
SPEC §12.

## Decision

* **Fleet**: a named group of agents. An entry is written into exactly
  one of two new scope values: `self` (visible to the author agent only)
  or `fleet` (visible within the home fleet the entry was written into).
  **An entry's fleet membership is fixed at write time** — entries are
  immutable (ADR 0001) and are never re-parented.
* **Home fleet**: every active agent belongs to exactly **one** home
  fleet, assigned by the admin (and re-assignable by the admin at any
  time). One home fleet per agent in this increment; **multi-fleet
  membership is a named extension** — because entries reference fleets
  by id, re-parenting an agent (or adding membership in a second fleet)
  never touches existing entries.
* **Trust levels** (cumulative, 0–3):

  | Level | Name        | Reads                                              | Writes                       |
  |-------|-------------|----------------------------------------------------|------------------------------|
  | 0     | `untrusted` | nothing                                            | nothing                      |
  | 1     | `lurker`    | own entries + home fleet                           | own (`self`)                 |
  | 2     | `contributor` | own + home fleet                               | own + home fleet (`self`/`fleet`) |
  | 3     | `privileged` | own + home fleet + every fleet's entries         | own + home fleet only        |

  Level 3 is **read-broad, write-local**: it sees every fleet but writes
  only into its own home fleet.
* **Default scope**: a write without an explicit scope resolves to the
  **highest scope the writer's level permits** (L1 → `self`; L2/L3 →
  `fleet`). An *explicit* out-of-permission scope (e.g. `fleet` at L1)
  raises a clear permission error naming the required level.
* **Level 0 (`untrusted`)**: all read verbs (`hive_search`,
  `hive_list`, `hive_get`) return empty results — the visibility filter
  hides everything; writes and feedback raise an explicit permission
  error. This is what pending (not-yet-activated) agents sit at.
* **Reads beyond visibility behave as if the entry does not exist** (no
  existence leaking).
* **Fleet moves**: when the admin moves an agent from fleet A to fleet B,
  the agent's earlier fleet-scoped entries **stay in fleet A** (they
  belong to the fleet they were written into; the agent, not the entry,
  was re-parented).
* **Legacy `scope='org'`**: grandfathered as a **read-only** scope
  value. No new write may use `org`; `org`-scoped entries remain
  readable by any identity at level 1 or above. (ADR 0002's value
  survives as a grandfather, not a write target.)
* **Fleet governance**: the admin creates fleets. **No fleet deletion in
  this increment** — deleting a fleet orphans its entries; rename/disable
  is the later story.

## Considered options

* **(a)** Keep the flat pool; add an opt-in `private` flag per entry —
  rejected: the flat pool offers no *read* isolation at all, and the
  default (share with the fleet, private on request) is the inverse of
  what the org wants.
* **(b)** Per-agent private pool + org pool with explicit promotion
  (Caura's original shape: a personal space whose entries are *promoted*
  to the shared pool) — rejected for this increment: promotion is a
  curation workflow (SPEC §10 holds it); the self/fleet scope choice at
  write time covers the stated need without a promote verb.
* **(c) One home fleet per agent + trust ladder — chosen.** Simplest
  shape that satisfies "an agent decides per write whether an entry is
  for itself or its fleet"; multi-fleet membership stays a clean
  extension because fleets are referenced by id.

## Consequences

* **Visibility is computed at read time** from (reader's trust level,
  reader's home fleet, entry's scope + fleet) — there are no per-entry
  ACLs, and `self`-scoped entries are private to their author **even at
  level 3**.
* `scope` values become `org` (legacy, read-only) | `self` | `fleet`;
  entries gain a fleet reference (a new column + `fleets` table).
* Feedback and withdrawal follow readability: an agent may feedback
  entries it can read, and may withdraw its own entries (plus, with the
  admin key, any entry) — the SPEC §4.1 withdrawal rules now ride the
  trust matrix.
* Multi-fleet membership, per-fleet promotion, and cross-fleet writes
  remain SPEC §10 extensions (the "namespaces/channels" and "private
  staging" lines are partially satisfied — see SPEC §10 notes).
* The `scope` filter on search/list accepts the new values; existing
  filters are unchanged.