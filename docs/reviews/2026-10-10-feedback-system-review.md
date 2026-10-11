# Feedback system review (2026-10-10)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Scope: the whole feedback loop on `main` at `fc9f387`: recording verdicts (single and batch), the quality multiplier search uses, readable feedback (ADR 0051), the `flagged` filter (ADR 0054), and the skill's guidance. Line numbers are on `main` at that commit.

## Verdict

The design is sound for what it is: a small, bounded, explainable outcome loop where the notes do most of the work. No security issue. No overhaul needed. There is one real scoring pathology (helpful reports "bank" credit that cancels later `wrong` reports), a handful of edge cases, and one clear-cut performance fix (batch feedback), which is in a PR. Elo or another pairwise rating does not fit: the system has no pairwise outcomes to rate, and too few verdicts per entry for any rating to converge (details at the end).

## How it works today

- One row per (entry, user, agent), latest verdict wins (`store/pgstore.py:257`, primary key in `schema.sql`). For an agent key, user and agent are both the agent's registered name (`store/auth.py:44`), so one agent has one vote per entry.
- Search multiplies each candidate's score by `quality = clamp(1 + 0.05·helpful − 0.10·stale − 0.25·wrong, 0.5, 1.2)` (`retrieval/scoring.py:44-45`, applied at `services/search.py:200`).
- Reads show the counts and the 5 newest reports with notes (`domain/feedback.py:44`, `store/pgstore.py:279`).
- `flagged=true` lists active entries with any stale or wrong report (`store/pgstore.py:364`).

## Findings

### 1. Helpful reports bank credit that cancels `wrong` reports (scoring pathology, medium)

The clamp is applied after the sum (`retrieval/scoring.py:44-45`), so every helpful report beyond the fourth is invisible until a negative report arrives, and then it absorbs it:

| helpful, stale, wrong | quality now |
|---|---|
| 4, 0, 0 | 1.20 |
| 20, 0, 0 | 1.20 |
| 10, 0, 1 | **1.20** |
| 20, 0, 3 | **1.20** (three agents said it is wrong; still maximum quality) |
| 10, 0, 2 | 1.00 |
| 0, 0, 2 | 0.50 (floor; a third or fifth report changes nothing) |

A popular entry that later goes wrong is the case the loop exists for, and it is the one the formula handles worst. The notes and the `flagged` list still surface it, but search keeps ranking it at full strength. This is a ranking change, so it waits for your choice (options below).

### 2. A stale or wrong note can drop out of view (medium)

`recent` is the 5 newest rows by `updated_at` (`store/pgstore.py:279`). Five later `helpful` reports, usually without notes, push the one `stale` report and its "what is true now" note out of the read. The reader sees `stale: 1` but not the correction, which defeats ADR 0051's purpose. Fix: rank rows with a note, or with a stale/wrong verdict, ahead of bare helpful rows (still capped at 5). Read-output change; waits for your choice.

### 3. Self-rating is not enforced (low)

The skill says "never rate your own" (`SKILL.md:114`) but the server accepts it: `record_feedback` checks readability only (`services/feedback.py:72`). An author's own `helpful` adds 0.05 and shows under its own name. Options: refuse it (`permission_denied`), or record it but leave it out of the counts. Behaviour change; waits for your choice.

### 4. The admin key can cast unlimited votes (low)

Admin keys have no `agent_id` (`store/auth.py:48`), so `record_feedback` uses the self-reported `agent` (`services/feedback.py:66`). Each distinct `agent` string is a new row, all shown as reporter `admin` (`api/schemas.py:153`). Ten calls with ten labels give ten votes. The admin panel does not offer feedback and the admin key is trusted, so this is low, but it contradicts "one reporter, one vote". A fix in the spirit of ADR 0060 would key admin feedback on `admin` alone. Needs a small ADR; waits for your choice.

### 5. Revoked agents' votes keep counting, and nobody can remove a vote (low)

Revoking an agent deletes its key (`store/auth.py:47`) but leaves its feedback rows, which keep shaping quality and keep entries flagged. There is no admin path to delete a feedback row; only the reporter (while active) can change it, or a contributor can supersede the entry. Options: an admin "clear feedback" action (audited), or exclude revoked agents' rows from counts. Waits for your choice.

### 6. Batch feedback: ~4 sequential round trips per id, not atomic (performance, fixed in the PR)

`record_feedback_many` checks each id one by one (`services/feedback.py:109`) and then calls `record_feedback` per id (`:114`), which reads the entry again, upserts and counts. A 16-id call is about 64 sequential queries and holds a pool connection across them in turn; a failure part-way leaves some rows recorded (ADR 0053 noted this). The PR does it in three store calls whatever the batch size: one readability read, one upsert of all rows in one transaction, one count read.

### 7. Ids are stored and echoed as the caller spelled them (bug, fixed in the same PR)

Both paths record `entry_id` as passed (`services/feedback.py:76, 86`) instead of the entry's canonical id. On Postgres an upper-case id is accepted but echoed upper-case, and in a batch the upper- and lower-case spellings of one entry are recorded twice and returned twice. The PR uses `entry.id`.

### Checked and fine

- Feedback follows readability; an invisible entry is `not_found`, same as unknown (`services/chain.py:37`).
- Visibility of notes: only through a read of the entry (ADR 0051).
- Query cost: counts and `flagged` use the `(entry_id, user, agent)` primary key; search loads counts in one batched query.
- Feedback on a withdrawn or superseded entry is accepted. Harmless (inactive entries are not ranked) and a `stale` on an old version is even accurate, so left alone.
- A successor starts at neutral quality; the predecessor's verdicts do not carry over. Correct: they were about the old text.

## Scoring options for finding 1

All keep "no feedback = 1.0" and the 0.5–1.2 bounds, and stay config values.

| helpful, stale, wrong | now | A: cap helpful on its own | B: consensus share |
|---|---|---|---|
| 1, 0, 0 | 1.05 | 1.05 | 1.07 |
| 4, 0, 0 | 1.20 | 1.20 | 1.13 |
| 20, 0, 0 | 1.20 | 1.20 | 1.18 |
| 0, 1, 0 | 0.90 | 0.90 | 0.87 |
| 0, 0, 1 | 0.75 | 0.75 | 0.67 |
| 0, 0, 2 | 0.50 | 0.50 | 0.50 |
| 5, 0, 1 | 1.00 | 0.95 | 1.00 |
| 10, 0, 1 | 1.20 | 0.95 | 1.08 |
| 20, 0, 3 | 1.20 | 0.50 | 1.04 |

- **A, cap helpful on its own:** `1 + min(0.2, 0.05·h) − 0.10·s − 0.25·w`. One-line change; removes banking. A lone dissent still costs 0.25 however many agents found the entry helpful.
- **B, consensus share:** `1 + 0.2·(h − 2s − 5w) / (h + s + w + 2)`, clamped. Each verdict moves the score by its share of all reports, with a prior of 2 so a single report cannot swing it far. A lone dissent among many helpful reports lowers the score; three among twenty pulls it back to near neutral; disputed entries end up in the middle, where the notes decide.

Recommendation: **B.** It fixes banking and treats a many-reporter entry by its balance of reports rather than raw counts. Either is a SPEC §4.2 change with an ADR, checked against the retrieval eval (which sets no feedback today, so it only needs a new pinned case).

Optional extra, not recommended yet: weight a verdict by the reporter's trust level (a lurker's `wrong` counting less). Lurkers flagging fleet entries is the point of ADR 0051, so the notes and the `flagged` list should stay unweighted even if quality is.

## Elo or a similar pairwise rating?

Short answer: no, not for entries.

- **Elo rates pairwise outcomes** ("A beat B"). Hivemind has none. A verdict is an absolute judgement of one entry by one agent; nothing ever says "entry A was more useful than entry B for this query".
- **The graph is agents × entries, and very sparse.** Most entries have no feedback, a few have one to three verdicts. Elo, Glicko or TrueSkill need tens of matches per item to settle; with three, the rating is mostly its starting value.
- **What could become pairwise data:** the hits a search returned, with the one the agent then relied on (rated helpful or superseded) counted as beating the others. That needs a record of every search's result list and of which hit was used. That is new write traffic on every search and close to passive capture (ADR 0004), and Caura's recall-count boost was already turned down for a similar reason.
- **Where "many-to-many" does have something to say is the raters, not the entries.** With agents rating entries, one can estimate how reliable each agent's flags are. The cheap, honest signal is already in the data: a `stale` or `wrong` report on an entry that was later superseded was probably right. A per-agent "flags later confirmed by a supersession" ratio is a plain SQL count, no new table. It belongs in the admin panel or `/v1/metrics` first, and becomes a vote weight only once there is enough data to see whether it separates good raters from noisy ones. That is the Dawid–Skene idea (rater reliability from agreement) at its smallest, and it fits the "smart, application-side, no new infrastructure" preference.

So the useful next steps are the formula fix (B), notes that stay visible (2), and the small integrity fixes (3–5). Elo would add machinery without the data to feed it.

## PR

- #97: batch feedback in one transaction with a fixed number of store reads, and canonical ids in feedback responses (findings 6 and 7).
- #100: consensus-share quality formula, option B (finding 1, ADR 0063).
- #99: stale, wrong and noted reports shown before bare helpful ones in `recent` (finding 2, ADR 0064).
- #102: the server refuses rating your own entry (finding 3, ADR 0065).
- #98: the admin key reports as `admin`, one vote per entry (finding 4, ADR 0066).
- #101: revoked agents' reports stop counting; rows kept, so reinstating restores them (finding 5, ADR 0067).
- #103: follow-up fix for main after #97 and #98 merged separately (batch path now uses the same reporter rule).
