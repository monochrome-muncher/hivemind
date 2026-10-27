# The agent portal: a browser front end for agent-key holders

> **Amended by [ADR 0113](0113-retire-an-agent-with-a-successor.md):** the portal's own-memory list and own-withdraw include the memories of agents retired in the caller's favour.

Amends the "a memory UI for anyone else is still a non-goal" line of SPEC §9 and the "Human read-only UI" row of SPEC §10, and the audit rule of SPEC §12.5 (an author's own withdrawal is not audited). Builds on [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md) (a static UI and an allowlisted proxy with the key held in the browser) and [ADR 0091](0091-admin-panel-shared-front-end-modules.md) (the shared front-end modules).

Status: Accepted.

## Context

The people who run agents have no way to see what their agents put into the pool or how the fleet judged it, short of `curl` with the agent's key or asking an admin. The admin panel shows all of it, but only to the admin key, and ADR 0029 keeps its proxy off the data plane so it can never relay an agent's key. ADR 0091 split the panel's widgets from the admin session so that a second app could reuse them. What was left open was the SPEC §9 decision and how such an app signs in, counts and audits.

## Decision

1. **A new runner, `hivemind-portal`** (`HIVEMIND_RUNNER=portal`), built like the admin panel: a static UI plus a same-origin proxy with its own allowlist, strict security headers and the key in the browser tab's `sessionStorage`. The proxy code is shared with the panel (`hivemind.webapp`). It holds no DSN and no Secret, never talks to Postgres, and the entrypoint skips the migration for it. Its settings are `HIVEMIND_PORTAL_API_URL` (required) and `HIVEMIND_PORTAL_API_CA_BUNDLE`.
2. **Agent keys only.** The portal signs a person in with one agent's key, checked with `GET /v1/whoami`. Admin and org keys are turned away with a pointer to the admin panel, and every portal route answers 403 to anything but an active agent's own key (a dev-mode identity included). One key at a time, in its own storage slot (never the admin panel's): to look at another agent, sign out and sign in again.
3. **Four agent-key routes**, each bounded by the key's own visibility (ADR 0011), so the portal shows nothing the agent could not already read:

   | Route | What it does |
   |---|---|
   | `GET /v1/portal/entries` | Lists what the key can read, newest first, with `kind`, `tags`, `scope`, `author`, `include_inactive`, `flagged` and `fleet_id` (so a privileged agent's home fleet can be shown alone) |
   | `POST /v1/portal/search` | `POST /v1/search` with a `fleet_id` filter, ranked the same, but **not counted** in the search counters (ADR 0056), so a person browsing does not move the agents' search numbers |
   | `GET /v1/portal/feedback?direction=given\|received` | The feedback log (ADR 0090) for this agent: the verdicts it gave, or the verdicts on its own memories, on entries the key can read |
   | `POST /v1/portal/entries/{id}/withdraw` | Withdraws one of the agent's own entries. A `reason` is required, and the withdrawal is audited as `entry.withdraw` with actor kind `agent_key`, actor the agent's name and detail `{author, reason, via: "portal"}`. Not even an admin key may withdraw someone else's entry here |

4. **The proxy forwards only** `GET /v1/whoami`, `GET /v1/entries/{id}` (the drawer, with `?history=true`), `GET /v1/pins` (the home fleet's pins), the four portal routes and `GET /v1/health`. Every data-plane write, the counted search and the admin surface are 404s from the portal itself.
5. **Pages:** the agent's memories (withdrawn and superseded included), the verdicts it gave, the verdicts on its memories, its home fleet's memories and pins, and search. Each list exports as CSV like the panel's (ADR 0094). They are the shared tables and drawer of ADR 0091; the drawer gained two options for it, `reasonRequired` and an `entry` argument to `feedbackHref`.
6. **Deployment:** `/v1/portal` joins `/v1/admin` on the example's locked source-range Ingress, off the public API host. Its own kustomization (`deploy/kubernetes/portal/`) with **two replicas, a `minAvailable: 1` PDB** and a `maxUnavailable: 0` rollout like the API (ADR 0026), because people other than the operator use it; a ClusterIP Service; and an example **internal** Ingress that no kustomization applies and that fails closed until edited. The portal is never put on a public ingress. The compose stacks run it on loopback (`:8090`, standalone `:8190`), `make portal` on `:8081`.

## Consequences

- SPEC §9 now allows two memory UIs, the admin panel's and the portal's; anything else stays a non-goal. The portal reads only what the agent's key already reads through the API, so it adds no new read right. Its one write, the withdrawal, is a right the author already had (SPEC §4.1).
- An author's withdrawal through `POST /v1/entries/{id}/withdraw` stays unaudited; one through the portal is audited, so an operator can tell a person's withdrawal from the agent's own. The audit log can now hold `entry.withdraw` rows with actor kind `agent_key`.
- A person holding an agent key sees everything that agent can read, its fleet's memories included. That is already true of the key; the portal makes it easy, which is why it belongs on an internal network only. The portal routes would also give an agent on the internet an uncounted search, so the example public Ingress keeps `/v1/portal` behind the same source-range Ingress as `/v1/admin` (the portal calls the API in-cluster and does not need it).
- No migration and no new audit action: `entry.withdraw` was already in the vocabulary. Upgrade `hivemind-api` before or with `hivemind-portal`; an older API answers 404 to the portal routes.
- The portal serves the admin panel's shared files (`ui.js`, `client.js`, `lists.js`, `csv.js`, `drawer.js`, `charts.js`, `admin.css`, `favicon.svg`) by exact name and none of the panel's other files. A change to one of them changes both apps, which `tests/unit/test_portal.py` and `tests/unit/test_admin_panel.py` both check.
