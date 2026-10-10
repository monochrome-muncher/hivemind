# Writes can be checked first, and answer with advisory warnings

> **Status:** amends [ADR 0052](0052-writes-report-the-nearest-entries.md).

## Context

ADR 0052 made every write report its 3 nearest entries, and rejected a
pre-write check verb: a ninth verb and a second embedding for what the
write's reply already carries. The price was that fixing a duplicate takes
a withdraw and a rewrite, which leaves a withdrawn entry and an audit
trail for every near-miss.

The hive-mind study (2026-10-10, plan rows 9 and 19) asked for two things
the write path can do cheaply: let a writer see the nearest entries
*before* committing, and point out mistakes that make an entry harder to
trust or follow (local paths, a successor dated before what it replaces, a
consolidation dated "today", sources dropped on supersede, a foreign
fleet's entry linked from a home-fleet one).

## Decision

1. **Check mode is a parameter, not a verb.** `hive_write` takes
   `check: true`; REST takes `POST /v1/entries?check=true`. A check runs
   every validation a real write runs (input bounds, scope resolution,
   supersede permission, see-also resolution), embeds the draft and
   answers `{checked: true, related, warnings}` with 200. It stores
   nothing and does not run the extractor. Errors are the ones the real
   write would raise. The tool count stays at ten.
2. **`related` carries more** on writes and checks: each item adds its
   `tags`, its feedback counts (`helpful`, `stale`, `wrong`, from one
   batched read) and `same_author`.
3. **Advisory warnings.** Writes and checks answer with `warnings`, a list
   of `{code, message}`. The codes are `local_path`, `foreign_link`,
   `backdated_successor`, `fresh_consolidation` and `sources_dropped`
   (SPEC §4.1). A warning never blocks or changes a write. Nothing is
   refused, merged or superseded automatically (ADR 0001, 0052 §3).
   `local_path` matches `/home/<name>`, `/Users/<name>` and `~/<name>`
   case-sensitively (so API routes like `/users/42` stay quiet; a
   lower-case `/home/<name>` is taken as a real home directory) and
   `C:\Users\<name>` case-tolerantly. `foreign_link` fires through
   `see_also` only: a foreign fleet's `supersedes` target is already
   refused (ADR 0033). `fresh_consolidation` asks the writer to date a
   restatement at the newest observation, and keeps today's date for an
   observation or re-check made today.
4. The warnings are computed from the supersede and see-also targets the
   write path already loads, so they add no store round trip.

## Consequences

- A careful writer pays a second embedding call (check, then write). A
  writer that skips the check pays nothing new.
- 0052's "pre-write check verb" alternative is superseded by item 1. Its
  other decisions stand.
- The always-on text grows by one sentence in `hive_write`'s description
  and one clause in the skill, inside the shared byte budget.
