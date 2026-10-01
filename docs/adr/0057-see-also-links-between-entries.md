# "See also" links between entries

## Context

Entries relate to each other in more ways than replacement. A decision
explains why a fact matters; a gotcha on one system points at the root
cause recorded for another. Supersession (ADR 0001) is the only link the
pool keeps, and it means "this replaces that". Everything else is left to
search: a reader of the older entry never learns that a newer one builds
on it, unless the right query happens to surface both.

Writers already try. The skill tells an agent writing a `self` note about a
fleet finding to cite the fleet entry's id as a source of type `other`. That
pointer is opaque: nothing resolves it, nothing checks it, and the entry it
names cannot see it.

A new `sources` type was considered and rejected. `sources` is decoded into
a closed enum, so an older replica reading an entry with an unknown type
would fail the whole read during a rolling deploy, and a rollback would
leave those entries unreadable.

## Decision

1. **A write may name up to 5 `see_also` entries** (`hive_write`,
   `POST /v1/entries`). Each must be an entry the writer can read (any
   state); otherwise the whole write is rejected as `invalid_input`, with
   the same answer for unknown and unreadable ids (ADR 0033), so a link
   cannot probe for entries in another fleet. Duplicates and spellings of
   one id collapse.

2. **Links live beside entries, not on them.** Migration `0011.entry-links`
   adds `entry_links(from_id, to_id, created_at)`, written in the new
   entry's transaction. Entries stay immutable (ADR 0001): a link is set
   once, by the entry that makes it. There are no foreign keys, so tools
   that truncate `entries` (older releases' test suites, ADR 0020's
   backward-compat check) keep working; the insert joins on `entries`
   instead.

3. **Reads by id show both ends.** `hive_get` (single and batch),
   `GET /v1/entries/{id}` and `POST /v1/entries/get` add `see_also`, the
   entries this one links to, and `linked_from`, the active entries that
   link to it (at most 20, newest link first). Each is compact: id, kind,
   summary, author, state, fleet. Both are limited to what the reader may
   read: a link to or from an entry the reader cannot see is left out, as
   if it did not exist.

4. **Links do not affect ranking.** Search and list are unchanged; this is
   navigation for the agent that opened an entry.

## Consequences

- An agent that opens an entry sees the newer entries that build on it,
  which search alone would not show.
- One more table and two indexed reads per opened entry.
- A link outlives what it points at: a link to a superseded entry stays,
  with the target's state shown, and `include_history` leads to the
  current version. `linked_from` skips inactive linkers, since a
  withdrawn or superseded entry is no longer a reason to look.
- The `self`-note-cites-fleet-entry pattern can use `see_also` instead of
  an `other` source.
