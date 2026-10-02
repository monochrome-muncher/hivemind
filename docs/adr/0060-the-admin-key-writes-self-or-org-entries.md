# The admin key writes `self` or `org` entries

> Amends [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md)
> (an admin supersedes anything).

## Context

The admin key is meant for the admin surface, but it can also write
entries, with `author: admin` and a self-reported `agent`. It has no home
fleet, so a `fleet` write from it was stored with no fleet: readable only
by its author and by privileged agents. ADR 0033 let an admin supersede
anything, with no audience rule. An admin who corrected a fleet entry with
a `fleet` (or `self`) successor therefore hid the entry from the fleet's
contributors and lurkers. The predecessor left search as superseded, the
successor was unreadable to them, and a pin on it stopped at the old
version.

## Decision

1. **The admin key writes `self` or `org`, never `fleet`.** An explicit
   `scope: "fleet"` is refused as a permission error (REST 403, MCP
   `permission_denied`). The omitted-scope default stays `org`.
   `whoami` lists `self` and `org` as its writable scopes.
2. **An admin supersession must reach the target's readers.** An admin may
   still supersede any active entry, but the successor must be `org`
   (which every reader at level 1 and up reads), unless the target is the
   admin's own `self` note, which any successor reaches. This is ADR 0033's
   audience rule, applied to the admin key.

## Consequences

- An admin correction of a fleet entry is an `org` entry: the fleet keeps
  reading it, and so does every other reader.
- No entry can be written with `scope: fleet` and no fleet any more. Rows
  already written that way stay as they are, readable as before.
- Legacy (dev-mode, unauthenticated) credentials share the admin's read
  bypass, so the supersession rule applies to them as well. They can still
  write `fleet` with no fleet, as before.
