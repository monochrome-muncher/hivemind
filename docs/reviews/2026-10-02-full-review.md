# Hivemind security and performance review (2026-10-02)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Scope: `main` at 107d827 (v2.3.1). Covers the REST API, both MCP runners, the store and auth, search and history, plus the paths added since the 2026-10-01 reviews: related entries on write (ADR 0052), batch feedback (0053), the flagged filter (0054), batch reads (0055), search counters (0056), see-also links (0057), pins (0058) and Prometheus `/metrics` (0050). The plugins were reviewed in depth on 2026-10-01, and the plugin commits since then only change docs. Earlier findings are not repeated. Measurements used Postgres 16 + pgvector 0.8.6, with a 100k-entry pool where noted.

Short answer: there is no security issue, one performance issue worth fixing (fixed in PR #70), and two low items that need no action now.

## Findings, most severe first

### 1. Medium (performance / pool exhaustion): one batch read takes the whole connection pool. Fix: PR #70.
- `src/hivemind/api/routes.py:267-270` and `src/hivemind/mcp/app.py:526-529` call `asyncio.gather` on `feedback_summary` and `entry_links` once per entry. `feedback_summary` itself runs two concurrent store calls (`services/governance.py:351-355`), so 10 ids start about 30 store calls at once, each holding a pooled connection.
- The default pool is 10 connections per pod (`config.py:144`), and the acquire timeout is 10 s (`config.py:153`).
- Measured with 10 linked entries that have feedback: one batch read peaked at 10 connections (the whole pool). 50 concurrent batch reads took about 500 ms, with every other request queued behind them.
- Fix (ADR 0059, amends ADR 0055 §5): new `Store.list_feedback_many` and `Store.entry_links_many`, so a batch costs the same few reads for one id or ten. After the fix: a peak of 2 connections, 50 concurrent calls in about 90 ms, and one call in 4 ms instead of 22 ms. The response is unchanged.

### 2. Low (performance): reading a heavily linked entry sorts all of its incoming links. Not fixed.
- `store/pgstore.py` `SELECT_LINKS_IN` orders by `created_at DESC LIMIT 20`, but `entry_links_to_idx` (migration 0011) covers only `to_id`, so every link to the entry is read and sorted.
- Measured: 50 000 links to one entry (10 000 writes with 5 links each) cost 9 ms per read of that entry, and the cost grows linearly. Each write needs an embedding call, so building this up is slow and visible.
- A possible fix is a new index `(to_id, created_at DESC, from_id)` built `CONCURRENTLY` in a new migration. It isn't worth a migration until links are actually used heavily.

### 3. Info: batch feedback does about 4 sequential round trips per id and is not atomic against store failures.
- `services/governance.py:328-343` checks each id with its own read, then `record_feedback` reads it again, upserts and recounts: about 64 round trips for 16 ids (measured 10 ids in about 22 ms). If the store fails partway, some verdicts are already recorded. A retry repairs this, because the upsert is idempotent. No change proposed.

## Checked and not an issue
- **Visibility on the new paths.** Related-on-write uses the writer's visibility in SQL (`similar_entries`), and see-also targets must be readable before linking, with "unknown" given for invisible ones (`_resolve_see_also`). Outgoing and incoming links are filtered by the reader's visibility, and incoming ones by state too (`chain.entry_links`). Pins check readability before permission (`_pinnable`), list only readable entries and follow supersession only through readable successors (`_current`). Feedback rows are shown only after the entry passes `get_visible_entry` (ADR 0051).
- **Org key and level-0 keys** get `FALSE` from the visibility clause, so they cannot read, pin, link or give feedback on anything. They can bump the search counters for "no fleet", but those are counts only.
- **Pin cap race.** The per-fleet advisory lock wraps the count and the insert (`pin_entry`). Arbitrary `fleet_id` strings are safe because `pins.fleet_id` is `text`.
- **Flagged filter.** The planner hash-semi-joins the stale/wrong feedback rows, so the query does not scan entries: 3 ms on 100k entries with 30k feedback rows.
- **Search counter upsert** (`RECORD_SEARCH`): it is one autocommit statement per first-page search, so the row lock is held for microseconds and the update is HOT. It is best-effort and never fails a search.
- **Prometheus `/metrics`.** It has no auth by design and is kept off the public Ingress. The usage scan is cached for 30 s with one refresh at a time. Route labels use templates and per-author series cover registered agents only, so cardinality stays bounded.
- **Auth.** Keys are SHA-256 lookups, and agent keys work only while the agent is active (one joined query). Admin-only routes are unchanged since the 2026-10-01 reviews.

## Still open from earlier reviews
- The known items in ROADMAP §3.13 (for example rate limiting registration by org key). One common keyword term still costs about 85 ms on 100k rows (ROADMAP §4.1).

## Bug hunt and logic review (added later the same day)

### 4. Bug: once a pinned entry is superseded, unpinning the id the briefing shows does nothing, and pinning it pins twice. Fix: PR #71.
- The briefing shows the newest version's `id` (ADR 0058, `GovernanceService.pinned`). `unpin` deletes only the pin on that exact id, so it answered `removed: false` and the pin stayed. Only `pinned_id` worked, and the skill doesn't mention it.
- Pinning the shown id added a second pin: the briefing listed the entry twice and used two of the ten slots. Two pinned entries merged into one successor also showed twice.
- Reproduced in-process on main. PR #71 makes a pin answer to the version it shows and de-duplicates the briefing.

### 5. Logic flaw: an admin's `fleet` write landed in no fleet. Fixed in PR #72 (ADR 0060), as the maintainer decided.
- `resolve_write_scope` (`services/access.py`) leaves `fleet_id` empty for the admin key, which has no home fleet. Such an entry is visible only to its author and to privileged agents.
- Admins "supersede anything" (ADR 0033). So if an admin supersedes a fleet entry with a `fleet` successor, the fleet's contributors lose it: the predecessor is superseded and the successor is unreadable to them. A pin on it then stops at the old version.
- Decision: the admin key writes only `self` or `org`, and an admin successor must be `org` unless it replaces the admin's own `self` note. Merged in PR #72.

### 6. Info (unreachable today): `AccessService.activate` flips the status before checking that an authenticator exists.
- With no authenticator, the agent would end up active with no key, and a retry would answer 409. `revoke` checks first. Activation is REST-only, and REST always has an authenticator, so this can't happen today.

### 7. Info: MemoryStore orders search ties differently from Postgres.
- `memstore.search_keyword` / `search_vector` break ties oldest first, while `PgStore` breaks them newest first (`created_at DESC`). This only affects the dev and test store.

Also checked, no issue: may_supersede across trust levels and fleet moves; write-scope resolution; withdraw rules; the feedback upsert identity; the search pagination ceiling (documented in SPEC §6); the scoring math (future dates clamp, the quality bounds); the PATCH half-apply guard; registration races.
