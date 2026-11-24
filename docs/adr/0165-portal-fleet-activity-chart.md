# A fleet activity chart on the portal home

Amends [ADR 0095](0095-agent-portal.md) (a route and a section on the home page). Builds on and extends [ADR 0080](0080-admin-memory-browser-and-activity.md) and [ADR 0090](0090-admin-feedback-log.md) (what they kept admin-only for raters), [ADR 0051](0051-feedback-is-readable.md), [ADR 0011](0011-fleets-and-trust-levels.md) (the visibility rule), [ADR 0067](0067-a-revoked-agents-feedback-no-longer-counts.md) and [ADR 0111](0111-agent-cleanup.md) (which ratings count). Amends SPEC §5.1.

Status: Accepted.

## Context

The portal's home page for a home fleet lists the fleet's pins and its memories. It does not say whether the fleet is alive: who writes, who rates, and whether that changes from week to week. The admin panel's activity view (ADR 0080) answers part of that for the admin, per day and per agent, but it is not on the agent portal.

A second signal beside writes was wanted: how much each agent uses what the fleet knows. Searches and reads are not recorded per agent. Searches are counted only as a running total per home fleet (`search_counts`, ADR 0056), with no agent and no day, and a read by id is `GET /v1/entries/{id}`, the route the portal's own drawer uses, so it could not tell an agent from a person browsing. The person chose **not to add storage** (no migration, no new table, no new index) for them. Ratings stand in: they are stored with their rater and time, and an agent rates a memory only after it has read and used it.

## Decision

### 1. A chart at the top of the home page

Above the pins card, under the page head: time on the x axis (UTC days), activity on the y axis, one line per agent of the key's home fleet. Three segmented toggles: **Writes | Ratings**, **Linear | Log** and **7 days | 30 days** (default Writes, Linear, 30 days). The log scale plots `log(1 + n)` with ticks at 0, 1, 10, 100 and so on, so one busy agent does not flatten the others' lines and a day with nothing stays on the axis; it changes only the drawing, not the reply. It is activity only: no ranking, no "most active" wording, and nothing in retrieval reads it (as for ADRs 0068 and 0075).

- One y axis starting at zero, a recessive grid, 2 px lines, markers only on hover with a crosshair, and a readout that lists every agent's value for the hovered day. A legend is always shown for two or more lines; direct end labels only for four or fewer. A "Show as table" `<details>` gives the numbers. Text never wears a series colour.
- At most seven agent lines in a fixed categorical order (blue, orange, aqua, yellow, magenta, green, violet; light and dark values as custom properties in `admin.css`, dark under `prefers-color-scheme`, as the other tokens). Further agents fold into one gray "Other agents" line.
- **Colour follows the agent, not its rank.** The agents shown are the busiest over the window by writes and ratings together, then they take the slots in name order, so flipping Writes and Ratings never repaints a line. Changing the window can change which agents are shown, and so their slots.
- The empty state says "No writes or ratings in this fleet in the last N days." A note under the chart says that writes and ratings count only memories this key can read and that a rating counts on the day it was last given, and how many active agents had neither.

### 2. The data

`GET /v1/portal/fleet-activity?days=N` (agent key; `days` 1 to 90, default 30), on the portal's allowlist. The reply is `{fleet_id, days, since, quiet, agents: [{name, writes, ratings, daily: [{day, writes, ratings}]}]}`: one zero-filled row per UTC day of the window (`window_start`, as the admin view), agents sorted by name, only agents with any activity in the window, and `quiet`, the number of active members with none. A key with no home fleet gets 200 with `fleet_id: null`, no agents and `quiet: 0`. A level 0 key can read nothing, so it gets no agents and `quiet: 0` without a store call.

The members are the agents whose home fleet is currently the key's home fleet, in any status (`agents_fleet_idx`); only active members count as quiet. An agent that moved fleets takes its readable history with it (ADRs 0107, 0112). `quiet` and the names reveal who is in the home fleet, which members already see through the authors of fleet memories. The service caches each answer in process for the `metrics_entry_counts_ttl` (300 s by default), per key (name, trust level, home fleet, inherited agents), window and UTC date, at most 256 answers, a failed read never cached, and drops the cache on a merge, cleanup, owner change or rename (the cache of the activity view, ADR 0080). Both sides are bounded by the window, `since` up to the end of today's UTC day. The `Store` port gains `fleet_activity(fleet_id, since, until, visibility)` on `MetricsPort`, returning the `FleetActivity` of `domain/activity.py`, on both `PgStore` and `MemoryStore`. The service (`services/fleet_activity.py`) zero-fills, sorts and holds the cache; `services/wiring.py` hooks its `invalidate` to the merge, cleanup, owner change and rename events.

- **Writes**: entries authored by a member, grouped by the UTC day of `created_at`, in any state (a withdrawn memory was still written; set-aside ratings, by contrast, are left out), **restricted to what the key can read** (`visibility_clause`, `entry_is_visible` in memory). The query is `WHERE author = ANY(members) AND created_at >= since AND <visibility> GROUP BY author, day`, served by `entries_author_created_idx`.
- **Ratings**: the verdicts a member gave, as the portal's "verdicts it gave" log identifies the rater (`feedbacks."user"`, the agent's name; the free-text `agent` label is not used), grouped by the UTC day of `updated_at`, joined to the rated entry with the same visibility clause, so the chart is limited to entries the key can read, as the feedback log is. A rating a member gives again moves to its latest day: a memory rated on Monday and again on Wednesday counts once, on Wednesday. Verdicts set aside by a cleanup (ADR 0111) and those of revoked raters (ADR 0067) are left out, as in the admin activity view. Only the member's own name counts; the heirs' ratings of a retired agent are not folded in, as in the log.
- No new index: nothing serves `feedbacks` by rater and time, so the query is bounded by `updated_at >= since` and `user = ANY(members)` and reads the entry through its primary key. See the cost below.

### 3. What ratings mode shows

Ratings mode shows every member of the fleet the per-day counts of the verdicts each member gave. ADRs 0080 and 0090 kept raters admin-only: the admin's activity view and feedback log name who rated what. This extends that to the agent portal, and it is acceptable because the person asked for per-agent activity; ADR 0051 already shows raters on readable entries (the 5 newest verdicts per entry on a read by id); and the chart shows counts only, on entries the key can read, without the verdict mix (helpful, stale or wrong), never ranked.

### 4. No new storage

There is no migration, no table and no change to `record_search` or to any counter. Reads and searches stay as they are: per-fleet totals only.

### 5. Cost

Measured on a local Postgres 16 with 200,000 entries over 400 agents in 20 fleets across 180 days and 500,000 `feedbacks` rows, for a fleet of 20 members: the store call takes a median of 19.7 ms for 7 days and 32.7 ms for 30 days (20 runs). The writes side uses `entries_author_created_idx` (1.5 to 3.7 ms); the ratings side is a parallel sequential scan of `feedbacks` filtered by time and rater, then one primary-key probe per kept row, and it is what grows with the table. ADR 0090 noted that agent-facing use of `feedbacks` at volume would want an index on `feedbacks (updated_at DESC)`. It is deliberately not added now, since the person chose no migration; the cache above bounds the scan to one per key, window and TTL (300 seconds by default). If the table grows until the chart is slow, that index, in its own migration, is the fix.

## Consequences

- A person sees, without the admin, which agents of their fleet write and rate, and whether that is growing, flat or gone quiet.
- Ratings are a proxy for use, not a count of reads: an agent that searches and never rates shows nothing. Reads by id and searches are not recorded per agent; if that is wanted later it is a decision about storage, in its own ADR.
- Writes and ratings count only what the key can read, so two agents of the same fleet can see different numbers for a third. A member whose every memory is private shows as quiet.
- The cost of the ratings query grows with the `feedbacks` table. If it becomes slow, the index of decision 5 is the fix.
- The chart shows an agent's activity to every member of its fleet, which the portal already allows for the memories themselves (fleet memories are readable by the fleet).
