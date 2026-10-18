# The admin panel can read memories and show activity

Amends [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md)
(what the panel covers and what its proxy forwards) and the "not a memory UI" line of SPEC §9.

Status: Accepted. Extended by [ADR 0090](0090-admin-feedback-log.md) (the feedback log) and [ADR 0092](0092-admin-fleet-pins.md) (fleet pins).

## Context

The admin panel (ADR 0029) manages agents, fleets, the audit log and the
org key. An operator running it also wants to see what the pool holds and
how it is used: which memories an agent wrote, what a fleet is working on,
who is active, how much gets written and rated each day, and how that
splits by fleet, agent and the person who owns the agents. Until now the
only way to read an entry as an admin was `curl` against the data plane
with the admin key.

SPEC §9 called the panel "not a memory UI", and ADR 0029 kept the proxy
off the data plane so that the panel could never relay an agent's own key
onto `/v1/entries` or `/v1/search`.

## Decision

1. **Five admin-only endpoints.** Each answers 403 to any key that is not
   an admin key, so the panel's proxy can forward them without becoming a
   relay for agent or org keys:

   | Endpoint | What it does |
   |---|---|
   | `GET /v1/admin/entries` | Lists entries with the `GET /v1/entries` filters plus `fleet_id`, newest first |
   | `GET /v1/admin/entries/{id}` | One entry in full: body, feedback with notes, see-also links, supersession history |
   | `POST /v1/admin/search` | Hybrid search with the `POST /v1/search` filters plus `fleet_id`; not counted in the search counters |
   | `POST /v1/admin/entries/{id}/withdraw` | Withdraws an entry, audited as `entry.withdraw` like any admin withdrawal |
   | `GET /v1/admin/activity?days=N` | Writes and verdicts per UTC day over the last N days (1 to 365), and per-agent and per-fleet totals |

   They add no new power. The admin key could already read and withdraw
   every entry through the data plane. The admin routes exist so the
   proxy allowlist can stay admin-only.

2. **Admin searches are not counted.** The search counters (ADR 0056)
   measure whether agents find what they look for. An operator browsing
   the pool would skew the empty-search rate, so `POST /v1/admin/search`
   leaves them alone. A degraded search (ADR 0048) still says so in its
   header.

3. **Activity is observation only, like the fleet health metrics (ADR
   0075).** Nothing in search, ranking, scoring, quality or anything an
   agent sees reads it. Feedback from revoked agents is left out, as in
   every other count (ADR 0067). The report is cached per window for
   `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` seconds, like the usage report
   (ADR 0069), so it can lag by that much. Verdicts are dated by
   `feedbacks.updated_at`, so a re-rated entry counts on the day of its
   latest verdict.

4. **"Users" are owner aliases.** Hivemind has no user accounts. The panel
   groups agents by the owner alias they registered with (§12.3) to show
   per-person figures. An alias is the registrant's own word, not a
   verified identity.

5. **The panel renders memories as plain text.** Entries are data written
   by agents, never instructions (ADR 0043). The panel shows summaries,
   bodies and source references through `textContent` only. Entry data
   never becomes an outside link: source references are shown as text,
   and the only links are the panel's own `#/memories?...` filters, so a
   stored entry cannot inject markup or lure a click off the page.

## Consequences

* SPEC §9 now allows an admin-only view of the memories in the panel,
  where the admin can read, search and withdraw. A human-facing memory
  UI for everyone else is still a non-goal.
* The proxy forwards five more admin paths. The data plane stays
  unreachable through it.
* The panel's page now shows entry content, which raises the cost of an
  XSS hole: it would expose memories as well as the admin key. The CSP
  and the `textContent` rule from ADR 0029 still carry the defence, and
  the tests that enforce them now cover every script in the bundle.
* The activity queries are grouped scans over `entries` and `feedbacks`,
  cached for the TTL. They run only when an admin opens a page that needs
  them.
