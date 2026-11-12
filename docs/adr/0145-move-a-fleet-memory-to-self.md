# Move a fleet memory to self

Amends [ADR 0001](0001-append-only-entries-with-explicit-supersession.md) (entries are append-only) for one field, builds on
[ADR 0011](0011-fleets-and-trust-levels.md) (scopes), [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md)
(a supersession never narrows its audience), [ADR 0080](0080-admin-memory-browser-and-activity.md),
[ADR 0095](0095-agent-portal.md), [ADR 0096](0096-contributors-pin.md),
[ADR 0113](0113-retire-an-agent-with-a-successor.md) and [ADR 0141](0141-every-withdrawal-is-audited.md).

Status: Accepted. Points 3 and 4 amended by [ADR 0149](0149-move-to-self-follows-inheritance.md): the versions of every agent the author inherited from count as its own.

## Context

An agent sometimes writes to its fleet what should have been a private note: a scratch thought, a
personal preference, something about its own session. Until now the only way to take it out of the
fleet's sight was a withdrawal, which also takes it away from its author. The owner asked for a
narrower fix: the admin, and the person behind the agent, can move the memory to the agent's `self`
scope, so it stays active for its author and nobody else sees it.

## Decision

1. **One field changes: `scope`, from `fleet` to `self`.** Text, author, fleet id, dates, state,
   feedback and supersession links stay. Only an **active fleet** memory can be moved; `self` and
   legacy `org` memories, and superseded or withdrawn ones, are refused with 409 `conflict`. The
   fleet id is kept, so the record still says where it was written; for a `self` memory it decides
   nothing (§12.2).
2. **Who.** The admin key moves any agent's memory: `POST /v1/admin/entries/{id}/move-to-self`.
   The person behind an agent moves the memories that agent owns, its own and those it inherited
   (ADR 0113), from the agent portal: `POST /v1/portal/entries/{id}/move-to-self` (agent key only).
   There is no MCP tool: agents keep their ten tools, and an agent that wants a private copy writes
   one. Both routes need a `reason`.
3. **Earlier versions by the same author move with it.** Following `superseded_by` backwards from
   the memory through versions written by its author, every `fleet` version moves to `self` too, so
   the fleet is not left reading an old version of something it can no longer see the head of.
4. **A memory that replaced another agent's fleet memory is refused.** If the memory, or one of
   those earlier versions, superseded a fleet memory by someone else, the move is refused with 409
   and nothing changes; the admin can still withdraw it. Moving it would hide the line of another
   agent's memory behind a private note the fleet cannot read, against ADR 0033. The owner chose
   this over putting the other agent's version back, or leaving it hidden.
5. **Pins on every moved version are removed**, in the same transaction, and audited as
   `entry.unpin` with `reason: "moved_to_self"`. The pin sweep and the briefing also treat a pin whose
   current version is `self` like one on a withdrawn memory, for a pin written while the move ran.
6. **Ratings and notes stay.** They keep counting for the author's own searches. The agents that
   rated it stop seeing it, so it drops out of their lists, "relied on" included, as any memory
   they can no longer read does.
7. **Audited as `entry.move_to_self`**, target the memory id, detail `{author, fleet_id, reason,
   earlier_versions, via?}` (`via: "portal"` from the portal). It shows in the agent's History in
   the admin panel and the portal. Migration `0035` widens `audit_log_action_check` and adds a
   partial index for the History query, the way `0031` and `0032` did.
8. **No undo.** To share it again, the author supersedes the private note with a fleet memory,
   which §4.1 already allows (a `self` memory may be replaced at any scope).
9. **A `self` memory is only superseded by an owner or an `org` successor, in the store too.** The
   write path checks `may_supersede` before its transaction, so a write aimed at a fleet memory could
   land just after the move. The guarded supersession flip now refuses a `self` target unless the
   writer is its author, an heir, or the admin writing `org`, which is what `may_supersede` already
   allows.

10. **Undo paths respect the move.** A cleanup undo replaces a memory it had put back only if that
    memory is still a fleet one, or the replacing version is its owner's; otherwise it counts the
    memory as skipped. A cleanup, merge or move undo puts no pin back on a memory now `self`.

## Consequences

- The fleet loses the memory at once, with no notice to the agents that read or rated it, as with a
  withdrawal.
- The move locks the author's cleanup lock, then the fleet row shared (as a fleet write), then the
  rows; a merge, a move of the fleet, or a cleanup of the author runs wholly before or after it.
- Risk class: permissions and a data change. Tested on both stores (the chain, the refusal, the pins,
  the supersession guard with an heir and an admin) and through both routes.

## Alternatives considered

- **Withdraw and have the agent rewrite it as `self`.** Works today, but takes the memory away from
  its author and loses its ratings. The owner wanted something different from a withdrawal.
- **Put back the other agent's version it replaced.** Cuts a supersession link outside a cleanup
  and makes a claim nobody made. Rejected by the owner in favour of refusing.
- **Allow `org` to `self`.** Legacy `org` memories are read-only (ADR 0011) and not asked for.
