# Feedback is readable by the entry's audience

## Context

Feedback (SPEC §4.2) was write-only. A reporter's verdict and `note` were
stored in `feedbacks`, but no read returned either: search hits and reads
by id carried no feedback field, and the verdict counts search loads for
every candidate were folded into the quality multiplier and dropped. The
only effect of a report was that multiplier, bounded below at 0.5, so an
entry three agents had reported `wrong` still ranked, and still read as
authoritative to the next agent that opened it.

The `hivemind` skill already assumed otherwise. It tells a lurker, which
writes only to `self` and so cannot supersede a fleet entry, to flag an
outdated one with `stale` or `wrong` and "a `note` saying what is now
true". That correction reached nobody.

## Decision

1. **Search hits carry the verdict counts**: `feedback: {helpful, stale,
   wrong}` on REST `POST /v1/search` and MCP `hive_search`. Search already
   loads these counts for scoring, so this adds no query.

2. **A read by id carries the counts and the newest reports**:
   `feedback: {helpful, stale, wrong, recent: [...]}` on REST
   `GET /v1/entries/{id}` and MCP `hive_get`, where each report is
   `{verdict, note, reporter, updated_at}` and `recent` holds at most the
   5 newest rows (`FEEDBACK_RECENT_LIMIT`), newest first. The counts cover
   every reporter; the cap bounds what one read adds to an agent's
   context (a note is at most 2000 characters, ADR 0040). It is one
   indexed query on the `feedbacks` primary key, through a new
   `Store.list_feedback`. `reporter` is the reporter's registered agent
   name, the same identity entries carry as `author` (ADR 0012).
   Supersession-chain entries returned with `?history` / `include_history`
   do not carry feedback; open a version to read its own.

3. **Feedback is shown to exactly the audience that can read the entry.**
   It is only ever returned through a read of the entry, and those reads
   follow readability (ADR 0033), so no new visibility rule is needed.

4. **Notes are untrusted data, like entries (ADR 0043).** The server's
   MCP instructions list feedback notes among the content an agent must
   never take instructions from, and the skill says the same.

## Consequences

- A stale or wrong report, and the correction in its note, reaches the
  next reader before it relies on the entry. A lurker's flag on a fleet
  entry becomes useful to the contributors who can supersede it.
- A note is now written for an audience. A reporter can read an entry it
  may not write to (a lurker reads its home fleet; a privileged agent reads
  every fleet), so its note reaches readers it could not otherwise write
  to. That is the point for a lurker. For a privileged agent it is a
  channel into another fleet, and the foreign-entry rule (ADR 0036, "use,
  don't relay") now covers it: the skill tells a privileged agent not to
  carry its home fleet's findings into notes on foreign entries. Nothing on
  the server enforces this, as with ADR 0036 itself.
- Notes written before this change were written for nobody and are now
  shown. They are bounded, attributed and framed as data; no migration
  rewrites them.
- Hits and entries gain a field. Clients that ignore unknown fields are
  unaffected; the REST hit list stays a list.
- A "needs attention" listing (entries reported stale or wrong with no
  successor) builds on this and is left for its own change.
