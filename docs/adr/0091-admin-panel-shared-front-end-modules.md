# The admin panel's generic modules know nothing about the admin

Amends [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md) (how the panel's static UI is organised). No behaviour change for the admin.

Status: Accepted. [ADR 0094](0094-admin-csv-export.md) adds a sixth shared module, `csv.js`. [ADR 0095](0095-agent-portal.md) is the second app: the agent portal serves these modules as they are.

## Context

A second front end is being considered: an app where a person signs in with an agent key and sees what that agent did (memories written, feedback given), withdraws its own memories and reads its home fleet's memories. That app is not decided (it touches SPEC §9 and the agent-key trust surface), but much of what it would show is already in the admin panel: memory rows, the memory drawer, feedback rows, badges and formatting. Until now every panel module imported `core.js`, which mixes those widgets with the admin key's storage slot, the admin routes and the sign-out on 401.

## Decision

1. **Five modules import nothing admin-specific**: `ui.js` (DOM helpers, formatting, badges, modal, layout pieces), `client.js` (the API client, made per app with its own `sessionStorage` slot), `lists.js` (paged memory and feedback tables), `drawer.js` (the memory drawer) and `charts.js`. They take what they need as arguments: a page fetcher, an "open this memory" callback, a fleet-name lookup, href builders.
2. **The drawer is configured by the app** (`configureDrawer`): how to fetch an entry, whether and how to withdraw one, where the author, fleet and feedback links go. With no withdraw function there is no Withdraw button.
3. **`core.js` keeps the admin half** (the admin key slot, `guarded`'s sign-out, the agent and fleet cache, the admin routes the views share) and re-exports `ui.js` and `client.js`, so the views import from one place as before.
4. **The panel gains an agent page** (`#/agent?name=…`) built from those pieces: registration, numbers, the memories it wrote, the verdicts it gave and the verdicts its memories got. It is the admin's view of the same page an agent-key app would show for its own agent.

## Consequences

- A second app can serve these five files beside its own entry module, with its own client, routes and drawer configuration. It would still need its own runner and proxy allowlist (ADR 0029 forbids the admin proxy from relaying the data plane), and its own decision on SPEC §9.
- The admin key never shares a storage slot with another app's key: each app makes its own client.
- `tests/unit/test_admin_panel.py` keeps its rules over every module: no HTML sink, sibling imports only, every API path on the allowlist, and hrefs only from the in-panel hash helpers in `ui.js`.
