# Review of the changes since 2.23.1

Read on main at b627590 (merge of #404), compared with v2.23.1 (49e8107). It covers #393 (verdict counts in the memory lists), #394 (the admin panel and portal proposal), #395 to #399 (portal "Relied on" page, admin search as an agent, owner page and offboarding, "why this rank" in the admin and portal searches; ADRs 0135 to 0139), #400 (the `not_english` quote regex in linear time), #401 (fleet page), #402 (memory filters, ADR 0140), #403 ("since you last looked" and the portal's "new" badges) and #404 (every withdrawal audited, ADR 0141).

**Result: no security issue, two small bugs and one deferred item, each fixed in its own pull request. No docs drift.**

## Findings

| # | Severity | Where (b627590) | Finding | Fix |
|---|---|---|---|---|
| 1 | Low (bug) | `src/hivemind/admin/static/since.js:97` | The Overview's "since you last looked" line counted every audit row that was not a withdrawal or counted elsewhere as an "other admin action". Agents' own pins, unpins and portal cleanups are audited with an agent key (ADRs 0076, 0116, 0118), so a busy fleet read as the admin having done things. | Rows with actor kind `agent_key` stay out of that count; withdrawals still count every withdrawal. PR #405. |
| 2 | Low (bug) | `src/hivemind/api/routes/admin_agents.py:182`, `src/hivemind/admin/app.py:68` | An owner alias may hold a `/` (`validate_owner_alias`, `domain/validation.py:250`, refuses only control and zero-width characters), but the offboard route and the proxy allowlist took one path segment. The owner page of `ops/jane` offered "Offboard this owner…" and the preview failed with a bare 404 (checked through the HTTP seam, raw and as `%2F`). | `{alias:path}` on the route and `admin/owners/[^/].*/offboard` on the allowlist; the proxy already re-quotes each segment and refuses dot segments. Runbook section 17 says how to put an alias in the URL. PR #406. An alias that is exactly `.` or `..` still cannot go in a URL; noted only. |
| 3 | Minor (deferred from #399) | `src/hivemind/portal/static/views.js:432` | With "Explain ranking" ticked, the portal search's CSV export asked the search to explain every exported page, which the CSV never shows. | `pagedTable` hands export pages `exporting: true`; the portal search leaves `explain` out of them. PR #407. |

## What was checked and holds

| Area | Result |
|---|---|
| Search as an agent (ADR 0136) | Admin only (`require_admin` before the lookup, `admin_entries.py:133`). The credential is built from the agent row with no key (`store/auth.py:48`, `credential_for_agent`), only for an active agent; the authenticator contract test pins it equal to what `verify` returns. The admin already reads everything, so it cannot widen access. `count=False`, so no search counters move and nothing is recorded for the agent. |
| Why this rank (ADRs 0137, 0139) | `score_parts` is the one formula: `entry_score` returns its `.score`, so explaining cannot change ranking. Keyword and vector ranks come from the store streams, which are already visibility-filtered (`search.py:285`), so a portal key's ranks count only what it can read (test in `test_portal_api.py`). `explain` is left out of the JSON when not asked for; no agent surface carries it. |
| Offboarding (ADR 0135) | One transaction with the owner's and successor's rows locked in name order (`pgstore/offboarding.py:28`); the same `plan_offboarding` drives preview and apply, and `expected_agents` refuses a changed plan with 409. Key rows deleted, then one audit set per agent, as `retire` and `revoke` do. A retired-yet-active agent is revoked, not skipped. |
| Relied-on page (ADR 0138) | `RELIED_ON_CHANGES` (`pgstore/feedback.py:138`) repeats the conditions of `_relied_on_changed_clause` (`sql_common.py:125`); the memory store shares one helper for the filter and the facts. A successor id shows only when the key can read it (`hide_unseen_successors`). |
| Memory filters (ADR 0140) | `state` is an exact filter on the admin and portal list and search only; `EntryFilters.required_state` replaces the literal `state = 'active'` with a parameter. No partial index depends on that literal (`schema.sql`), so plans are unchanged. Portal list gains entities and dates, all still under the key's visibility. |
| Every withdrawal audited (ADR 0141) | The three withdraw paths (REST, MCP, admin) go through `GovernanceService.withdraw` (`governance.py:43`); the portal path keeps `via: "portal"`. The audit row is written after the withdrawal and a failed write propagates, as ADR 0027 asks. The portal history mapper's allowlist already handles `entry.withdraw`. |
| `not_english` regex (#400) | Timed every opener (straight, curly, low-9, guillemets, corner brackets, back quotes) at 35,000 unclosed repeats: each under 10 ms. |
| Front ends (#393, #401 to #404) | Agent text is set with `textContent` throughout (no `innerHTML` in any changed file); routes use `encodeURIComponent`. Chip buttons stop the click reaching the row. The "since" state is browser-only and every storage call is wrapped. |
| Docs | README, SPEC, CONTEXT, AGENTS, DEPLOY, the ops runbook (sections 15 to 17) and the ADR index all describe ADRs 0135 to 0141 as built. ADR 0027 and 0095 carry status notes. README additions use a plain voice with no em dashes. |
