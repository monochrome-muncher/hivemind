# A `flagged` filter for entries reported stale or wrong

## Context

Since ADR 0051, a `stale` or `wrong` report and its note are visible to
anyone who opens the entry. But nobody opens an entry to fix it unless
something leads them there. A lurker that flags a fleet entry cannot
supersede it (ADR 0011), and the contributors who can have no way to find
what was flagged, short of searching for the right words and checking each
hit's counts.

ROADMAP Tier 5 names the measure for the held Curation workflow:
"feedback (helpful/stale/wrong) accumulating without action". There was no
way to list that set.

## Decision

1. **`EntryFilters` gains `flagged`**: when true, only entries with at
   least one `stale` or `wrong` report. It is a public filter (SPEC §5.3)
   on `hive_list`, `hive_search`, `GET /v1/entries` (`?flagged=true`) and
   `POST /v1/search`.

2. **Combined with the default state filter, it is the queue of entries
   waiting to be fixed.** Superseding or withdrawing an entry makes it
   inactive, so it leaves the list; an entry stays while it is active and
   reported, whoever reported it and however long ago.

3. **The order is the filter's surface's usual order** (newest entry first
   on list, relevance on search). Ordering by report date or report count
   is not added: the queue is expected to be short, and an order would need
   a join the other filters do not.

4. **Feedback is not part of an `Entry`**, so `EntryFilters.matches` cannot
   evaluate this filter. Each store applies it beside `matches`: an
   `EXISTS` probe on the `feedbacks` primary key in Postgres, a feedback
   count in `MemoryStore`.

5. The skill tells contributors to check the list when they start work on
   a system and to supersede what they can confirm. It is guidance, not a
   workflow: there is no assignment, no state, and no new role.

## Consequences

- Flags reach the agents who can act on them.
- A `helpful` report later than a `stale` one does not unflag the entry:
  any stale or wrong report counts. The reports' notes say whether the
  flag still holds.
- An entry that was reported wrong but is in fact right stays flagged
  until someone supersedes it with a confirmation or the reporter changes
  its verdict (one verdict per reporter; the latest wins).
- This is a filter, not the Curation workflow of SPEC §10, which stays
  held.
