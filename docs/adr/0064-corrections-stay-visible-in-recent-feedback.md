# Corrections stay visible in an entry's recent feedback

## Context

[ADR 0051](0051-feedback-is-readable.md) made a read by id show the verdict
counts plus the 5 newest reports (`recent`). A reader who sees
`stale: 1` wants the note that says what is true now. But five later bare
`helpful` reports push that `stale` report out of the 5 newest, so the
count stays and the correction disappears.

## Decision

1. **Informative reports first.** `recent` lists, in this order: every
   report that is informative (verdict `stale` or `wrong`, or a non-empty
   `note`), then the bare `helpful` ones (`helpful` with no note or an
   empty note). Within each group, newest first (`updated_at` DESC), then
   reporter, as before. The list is still capped at `FEEDBACK_RECENT_LIMIT`.

2. **Both adapters, both reads.** `Store.list_feedback` and
   `Store.list_feedback_many` return rows in this order (the Postgres
   `ORDER BY` leads with `verdict = 'helpful' AND COALESCE(note, '') = ''`,
   false first, also inside the `row_number()` window), covered by the
   Store contract suite.

3. **Counts and shape unchanged.** The counts still cover every reporter;
   a report is still `{verdict, note, reporter, updated_at}`.

## Consequences

- A correction stays in `recent` until at least 5 newer informative
  reports exist; bare `helpful` reports can no longer displace it.
- `recent` is no longer strictly newest first across groups: a bare
  `helpful` report newer than a note can appear after it.
- No schema change, no new query: the same indexed read with a different
  sort key.
