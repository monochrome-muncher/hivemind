# Move to self follows inheritance

Amends [ADR 0145](0145-move-a-fleet-memory-to-self.md) points 3 and 4, builds on
[ADR 0113](0113-retire-an-agent-with-a-successor.md).

Status: Accepted.

## Context

ADR 0145 moves a memory with the earlier fleet versions *by its author*, and refuses the move when
any of them replaced a fleet memory by *another agent*. An heir that superseded a memory it
inherited (ADR 0113) owns both versions, yet its predecessor counted as "another agent": the move
was refused, though the portal already lets the heir withdraw or move either version on its own.
The owner chose to let the move through.

## Decision

1. **The owners of a memory are its author and every agent it inherited from** (ADR 0113,
   transitively). The backwards walk takes every `fleet` version by any owner, and all of them move
   to `self`.
2. **The refusal is for a fleet version by anyone else.** A line in which a predecessor replaced a
   third agent's fleet memory is still refused, as before.
3. Nothing else changes: who may move (ADR 0145 point 2), pins, ratings, the audit row (its
   `earlier_versions` now lists inherited versions too) and the store-side supersession guard.

## Consequences

- An heir can take its whole line out of the fleet in one step.
- The move still takes the author's cleanup lock only. A cleanup of a predecessor that runs at
  the same time meets the move at the row locks, and a cleanup undo already respects a moved
  memory (ADR 0145 point 10).
- The audit row names the head's author in `detail.author`, as before, so a predecessor's own
  History does not show that its versions were narrowed; the heir's History and the row's
  `earlier_versions` do.
- If the predecessor's retirement is undone later, its moved versions are `self` memories of the
  predecessor again, readable by it and the admin only. Nothing puts them back in the fleet, as for
  any move (ADR 0145 point 8).
- Tested on both stores: an inherited version moves, through two retirements too, its pins are
  removed, and a predecessor's version that replaced a third agent's memory still refuses the
  move.
