# Feedback on several entries at once

## Context

The skill asks agents to report every entry they relied on (SPEC §5.2).
A task usually draws on several entries, and `hive_feedback` took one id,
so reporting them cost one tool call each. Agents skip feedback when it
is expensive, and since ADR 0051 a skipped report is also a note no one
reads.

Caura's `caura_evolve` takes one outcome and up to 50 related memory ids
for the same reason.

## Decision

1. **`hive_feedback` takes `entry_ids` (up to 16) instead of `entry_id`**,
   with one `verdict` and one optional `note` for all of them. Passing
   both is `invalid_input`. REST gains `POST /v1/feedback` with
   `{entry_ids, verdict, note?, agent?}`, answering a list of
   `{entry_id, verdict, quality}`. The single-entry forms are unchanged.

2. **Each entry gets its own row**, exactly as a single report would write
   it: one row per (entry, reporter), latest verdict wins (SPEC §4.2).
   Repeated ids count once.

3. **All or nothing on readability.** Every id must name an entry the
   caller may read (ADR 0033), or nothing is recorded and the error names
   the ids that failed (MCP `not_found`, REST 404). A partial success would
   leave the agent to work out which reports landed.

4. **16 ids at most** (`MAX_FEEDBACK_IDS`), the same bound as `supersedes`
   (ADR 0040): enough for one task's sources, and it bounds the work one
   call can cause.

## Consequences

- One call reports a whole task's sources, so feedback is cheaper to give.
- One note is shared by every entry in the call. A note specific to one
  entry still needs its own call.
- The calls are not one transaction: the readability check runs first,
  then each row is upserted. A store failure part-way leaves the earlier
  rows recorded; repeating the call is safe because feedback is an upsert.
- No outcome memory or generated rule is written, unlike `caura_evolve`:
  that would be passive capture (ADR 0004).
