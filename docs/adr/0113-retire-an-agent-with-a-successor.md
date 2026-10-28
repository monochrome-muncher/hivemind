# Retire an agent with a successor

Amends [ADR 0011](0011-fleets-and-trust-levels.md) (`self` entries are private to their author), [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) and [ADR 0034](0034-supersession-targets-must-be-active.md) (who may supersede), [ADR 0060](0060-the-admin-key-writes-self-or-org-entries.md) (the admin's own-note rule), [ADR 0065](0065-an-author-cannot-rate-its-own-entry.md) (an author cannot rate its own entry), [ADR 0095](0095-agent-portal.md) (an author withdraws its own entry from the portal) and [ADR 0096](0096-contributors-pin.md) (the per-agent pin cap), [ADR 0030](0030-hive-whoami-lets-an-agent-see-its-own-standing.md) (`whoami` gains `inherits_from`), and SPEC §4.1, §4.2, §5.1, §12.1, §12.2 and §12.5.

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

Agents get replaced: a new model, a new machine, a renamed workflow. The old agent's key is revoked, but its memories stay behind. Its `self` notes become unreadable to everyone, so nobody can supersede or withdraw them, and its fleet memories have no one who may fix or retract them. The only way to carry them over was to copy them by hand, which loses the history. Entries are immutable and `author` is provenance (ADR 0001, ADR 0012), so the answer cannot be to rewrite `author`.

Every access decision that today says "the author" compares `entry.author` to the caller's name: the read filter (SQL in `store/pgstore.py` and `entry_is_visible` in `domain/access.py`), `may_supersede`, withdraw and the portal's own-withdraw (`services/governance.py`), the refusal to rate (`services/feedback.py`) and the per-agent pin cap (`domain/pin.py`).

Decided with the user: the successor gets full ownership of the retired agent's memories. It reads, supersedes and withdraws them, sees them in its portal and cannot rate them (they count as its own).

## Decision

### 1. A succession is a row, not a rewrite

A new table `successions (retired_agent PK, successor, by, at)` (migration 0024). `retired_agent` is the primary key, so an agent hands down to one successor at a time; several agents may hand down to one. No FKs, like `pins` and `fleet_merges`. An undo deletes the row; the audit log keeps the history (`agent.retire`, `agent.retire_undo`). No memory is touched.

### 2. Ownership is `author in owned_authors`

A caller owns its own name plus every agent that hands down to it, transitively (A to B, later B to C: C owns A and B). `PgAuthenticator.verify` resolves this in the same query as the key lookup, with a correlated recursive CTE (`store/inheritance.py`), so a key check costs no extra round trip, and `UNION` keeps even a cycle from looping. The result is `Credential.inherits_from`, with `owned_authors` and `owns(author)` on `Credential` and `owns` / `inherited` on `Visibility`. `whoami` and `hive_whoami` show `inherits_from` (an empty list for most agents); the portal needs it to offer Withdraw.

Every ownership check now asks `owns(author)`:

- reads: `entry_is_visible` and the SQL in `_visibility_clause` (own `self` notes, own `fleet` memories in any fleet);
- supersession: `may_supersede` (through the visibility; the admin's own-note rule uses `owns` too);
- withdrawal: `GovernanceService.withdraw` and `withdraw_own` (the portal);
- rating: single and batch (`FeedbackService`), still after the readability check;
- the agent pin cap: the pins of the agents handed down count toward the successor's 3.

### 3. Ownership grants exactly what authorship grants

An author reads its own fleet memories wherever they sit, so the successor does too. Superseding a `fleet` memory still needs a replacement written into the same fleet (ADR 0033), which the successor can do only from that fleet. A successor in another fleet can read and withdraw such a memory, even in a fleet it is not a member of, but not supersede it. The preview says how many memories are in another fleet. Trust level and home fleet of the successor do not change.

### 4. Own name in `author` filters means everything owned

The skill tells agents to catch up with `hive_list` `author` = their `name` and `flagged: true`. To keep that working, `ReadService.list`, `SearchService` and the feedback log widen an `author` filter equal to the caller's own name to all authors it owns (`EntryFilters.authors`, `FeedbackLogFilters.authors`, set only by the services, never from input). Any other name stays literal, so a successor can still ask for what `old-bot` wrote. The admin never widens. This keeps the always-on skill text unchanged.

### 5. Pins

Unpinning is open to every member of the fleet (ADR 0096), so no ownership check is needed there. The cap is different: inherited pins count toward the successor's 3 (`pin_refusal(..., also_owned_by)`), and none is ever dropped. A successor who inherits three pins in its fleet cannot pin a fourth until it unpins one. Pins of a retired agent in another fleet cost it nothing there, because the cap is per fleet.

### 6. The operation

`POST /v1/admin/agents/{name}/retire` with `{successor, dry_run}` (`dry_run` required, like a merge). Admin key only, checked before anything is looked up. `dry_run: true` answers the preview: the active `self` notes, `fleet` memories and pins that pass over, how many fleet memories sit outside the successor's home fleet, the agents they come from (the retired agent and everyone who already handed down to it), and everything the successor would own. Otherwise, in one store transaction (`Store.retire_agent`, which also returns the status it saw under the lock; the revoke audit follows that): lock both agent rows in name order, check, insert the succession and set the agent `revoked`. Then the key row is deleted (as in a revoke) and two audit rows are written: `agent.revoke` (the usual one, detail `via: "retire"`) when the agent was active, and `agent.retire` `{successor, was}`.

Refusals: unknown agent 404; the successor not active, or the retired agent pending, 409 `invalid_status`; already retired, or the successor already hands down to the retired agent (a cycle), 409 `succession_refused`; the same agent twice 422. A revoked agent may be retired (its memories pass over; there is no second revoke).

`GET /v1/admin/successions` lists the standing ones. `POST /v1/admin/successions/{retired}/undo` deletes the row (404 when none) and is audited `agent.retire_undo`. It does not re-activate the agent; that needs a new key as for any revoked agent. While a succession stands, activating the retired agent is refused (409 `agent_retired`, in the API, the store and `hivemind-keys issue-agent`), so one memory never has two live owners by accident.

### 7. Admin panel

An active or revoked agent's row gets **Retire…**: pick the successor, read the preview, type the agent's name, confirm. The Agents view lists the standing retirements with Undo. The proxy allowlist gets the three routes and the audit filter the two actions. The portal's "Its memories" list and Withdraw button include inherited memories.

## Consequences

- A permission change with a small blast radius: the new ownership set is empty for every agent until an admin retires one, and the SQL is unchanged for them.
- `author` stays the original agent, so provenance, the audit trail and "who wrote this" are exact. A successor's supersession is a new entry authored by the successor.
- A successor's withdrawal of an inherited memory is audited as `entry.withdraw` with `{author, reason, via: "succession"}`, so the hand-over leaves a trail.
- The successor's earlier ratings on memories it now owns keep counting; only new ratings are refused. `upstream_changed` and the `same_author` related-entry flag still compare the literal `author`.
- An agent cleanup (ADR 0111) still goes by the literal `author`: cleaning up the retired agent withdraws what it wrote even though the successor owns it, and cleaning up the successor does not touch what it inherited.
- Undo takes everything back at the next request: nothing cached, the chain is read on every key check.
- Admin analytics (flag backlog, orphaned-author counts, activity) still group by `author`, so a retired author's flagged memories still show as orphaned. They are the admin's view of who wrote what, not an access decision.
- Rolling deploy: an older pod ignores successions, so a successor reads less there (fail closed), and it does not refuse activating a retired agent. Do not retire an agent during a rollout.
- Migration 0024 is expand-only (a table, an index, a widened CHECK). `make rollback` past it drops the table and the two audit actions' rows; retired agents stay revoked and successors lose the memories.
