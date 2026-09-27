# A supersession target must be active

## Context

ADR 0033 made supersession a claim within the audience the writer can
address: a writer may supersede an entry only if it can read it, and the
successor must reach at least everyone the predecessor reached. The
audience rule checks *who* may supersede, not *what state* the target is
in.

The store's flip is state-guarded — `FLIP_SUPERSEDED` updates only rows
with `state = 'active'` — and `superseded_by` is a **single** `uuid`
column. So a second entry that names an already-superseded (or withdrawn)
target passes the audience check, lands, and its claim is **silently
dropped**: the flip matches zero rows, the target keeps its first
successor, and nothing records that a second correction was attempted.

That orphaning is reachable without any race. Two agents independently
correcting the same stale entry — exactly the SPEC §2 "report when
something you relied on proved wrong" flow — leave one correction
invisible: `?history` shows only the first successor, and with
`include_inactive=true` the §6.3 ranking invariant ("the successor always
ranks above the predecessor") can be violated, because the second
successor is an orphan with no link to the target it corrected.

## Decision

**A write may name as `supersedes` only targets that are `active`.** A
target that is already `superseded` or `withdrawn` is rejected as part of
the whole write with the same `supersede_denied` ("not found or not
supersedable by you") error the audience rule uses, so the target's state
reveals nothing beyond reach. The check lives in `may_supersede`
(`domain/access.py`) beside the audience rule, so both surfaces (REST and
MCP) and the in-memory and Postgres stores share it; the state rule
outranks the admin bypass.

The writer's route is to re-target the **current head** of the chain:
supersede the entry that presently supersedes the original.

This amends SPEC §4.1, which lists `supersedes` as an entry field without
stating the target-state precondition, and ADR 0033's audience table,
which is now read as "may supersede an **active** entry …".

## Consequences

* A double-correction is now a loud 409-equivalent (`supersede_denied`)
  instead of a silently lost claim. Clients that catch the error can read
  the target's `?history`, find the head, and retry against it.
* The §6.3 ranking invariant and the `?history` walk are total again:
  every persisted `superseded_by` link corresponds to a persisted
  correction, so no orphan successors exist to break the "successor ranks
  above predecessor" guarantee.
* No schema change: `superseded_by` stays single-valued and the
  state-guarded flip is unchanged. The deeper fix — a `supersedes`
  id-array column driving both the invariant and the chain walk from
  persisted claims — remains available if real multi-agent contention on
  the same entry shows up; until then the rejection is the smaller,
  testable contract.
* Admins are not exempt: an admin superseding an already-superseded entry
  gets the same rejection, because the claim would be lost for the admin
  too.
