# Review: changes since v2.32.3 (2026-11-26)

Range: `1adafa5..21ae5c4` on main. That is #515 (MCP schemas state the input bounds, ADR 0166), the refactors #516 to #525, and the admin panel and portal refresh #526 and #527 (ADR 0167). 63 source files changed, about 1,000 lines each way.

## Verdict

There are no security issues, no bugs that can be reached, and no performance regression. The refactors keep the old behaviour, apart from the intended changes listed below. There is one low finding about the new verdict mix and three small readability or robustness nits. The nits (2 to 4) are fixed in the pull request that adds this review to `docs/reviews/`.

CI on main is green: Tests run 45878615243, Docker and CodeQL on `21ae5c4`, the #527 merge. #525's own run on main, 45861662184, also passed.

## Findings, ranked

### 1. Low: the fleet's verdict mix can be one agent's verdict mix

ADR 0167 says: "A fleet member can see the fleet's verdict mix, but not the mix of any single agent." That is not always true:

- `services/fleet_activity.py:94` adds up the verdicts of every member that rated in the window. `portal/static/fleetchart.js:59` and `:124` show the split whenever its total is above zero.
- If only one member rated in the window, the "fleet" mix is that agent's mix. The portal also shows per-agent rating counts beside it (`fleetchart.js:49`), so the reader can see who that agent is.
- In a two-agent fleet, the reader can subtract their own verdicts, which their "Verdicts given" page lists, and get the other agent's mix exactly.

What limits the harm: ADR 0051 already shows the five newest raters on any readable entry, so verdicts are not secret. This is a broken promise in the ADR, not a data leak.

Fix options:
- (a) Send `verdicts` only when at least three members other than the reader rated in the window, and show "Too few raters to show a mix" otherwise. This is a small change in the service, with a test.
- (b) Keep the behaviour and correct the ADR 0167 consequence line.

Decision (2026-11-26): (b). The pull request that adds this review corrects the ADR 0167 consequence line.

### 2. Nit: an orphaned comment in `update_agent`

`api/routes/admin_agents.py:111` says "The key first: a non-admin must not learn whether a fleet exists." The admin check it described moved to the router dependency in #521 (`admin_agents.py:37`). The comment now sits above a 422 check it has nothing to do with. Delete it.

### 3. Nit: `niceMax` is dead code with a stale comment

`admin/static/charts.js:14` exports `niceMax`, and its comment says "the portal's line chart still uses it". #527 removed the line chart. Nothing in `src/` or `tests/` calls it now (`grep niceMax`). Delete it.

### 4. Nit: `xLabelDays(0, w)` returns `[-1]`

`charts.js:38` returns `[-1]` for zero days, so `stackedBars` would throw on `daily[-1].day` (`charts.js:98-99`). Checked with node: `xLabelDays(0, 600)` gives `[-1]`. No real input reaches this. Both callers get rows filled with zeros for at least one day, and the portal draws no chart when there are no agents. A one-line guard (`if (!count) return [];`) makes the shared helper safe anyway.

## Intended behaviour changes (checked, not findings)

- **REST unknown write scope:** `POST /v1/entries` with a scope outside `self|fleet|org` now answers 422 `invalid_entry`, not 403 (`services/access.py:622`). This matches MCP `invalid_input` and ADR 0166 §4.
- **Case and spaces in MCP input:** MCP `kind`, `scope`, `verdict` and write `scope` are trimmed and lowercased. Ids in `supersedes`, `see_also`, `entry_ids` and `entry_id` are trimmed.
- **Order of 403 and 422:** on every admin and portal route, the key check now runs before body validation. A wrong key gets 403 before 422 (#521).
- **403 messages:** they are now generic ("this route requires an admin key", "the agent portal requires an agent key, …"). The codes are unchanged.
- **MCP refusals of missing agent identity:** `hive_write` and `hive_feedback` now come from the services' `AgentIdentityRequired` and still answer `agent_unresolved`. The REST twin still answers 422 `agent_identity_required` (`api/routes/entries.py:80`).
- **Offboard confirmation:** the typed confirmation now trims both sides, so an alias stored with spaces can be confirmed.

## Security checks

- **Route guards:** every route in `admin_agents`, `admin_audit`, `admin_cleanups`, `admin_entries` and `admin_fleets` now carries `require_admin_key`, and every route in `portal.py` carries `require_agent_key` (`api/routes/params.py:22`, `:27`). The 15 admin routes that had no route-level check before were already refused in the services (`services/access.py` `require_admin`). No `/admin` or `/portal` path lives in an unguarded router.
- **Store errors:** `UnknownEntry` subclasses `KeyError`, and `EntryNotActiveConflict` subclasses `ValueError`. Callers that catch the old builtins still work, and no service catches a bare builtin any more.
- **Credential SQL:** the statements moved to `store/credential_sql.py` unchanged, byte for byte. `insert_audit` keeps the caller's transaction.
- **Front end:** no new HTML-parsing sinks (`grep innerHTML|insertAdjacentHTML|outerHTML` finds nothing). Chart labels, agent names and tooltips are all set through `h()` or `svg()` with textContent.
- **Portal reply:** `verdicts` exists only at fleet level (`api/schemas/portal.py` `PortalVerdictsOut`). The per-agent rows keep only `writes` and `ratings`.

## Performance

The portal fleet activity query (`store/pgstore/metrics.py:103`) now splits ratings with `count(*) FILTER (...)`. I timed it on a throwaway database the size of the weekly perf check: 600,000 entries and 767,698 ratings over 180 days, an L2 reader in a 3-agent fleet, best of 6 runs of `PgStore.fleet_activity`:

| Window | v2.32.3 | main |
|---|---|---|
| 30 days | 116 ms | 119 ms |
| 90 days | 257 ms | 268 ms |

The plan is the same and the difference is noise. Answers are still cached for 300 s per visibility.

## Readability

The refactors read well. Each service is wired once in `services/wiring.py`, routes call the service they need, `as_utc` replaces seven copies, and the cleanup blocks and `typedConfirm` replace the copies in the JavaScript. Beyond nits 2 to 4 above, nothing needs a change.
