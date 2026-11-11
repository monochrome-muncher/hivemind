# Offboard an owner

Builds on [ADR 0113](0113-retire-an-agent-with-a-successor.md) (retire an agent with a successor), [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md) (revocation) and [ADR 0122](0122-admin-corrects-an-agents-owner-alias.md) (the owner alias). Touches [ADR 0027](0027-audit-log-of-admin-actions.md). Adds SPEC §5.1 `POST /v1/admin/owners/{alias}/offboard`, the §12.1 note on offboarding and a page to the admin panel.

Status: Accepted.

## Context

When a person leaves, the admin has to deal with every agent they registered. Today that is one **Retire…** or **Revoke** per agent, each with its own dialog. A person with six agents means six dialogs, and it is easy to miss one: an agent whose key still works after its owner is gone. The admin panel also has no place that shows one owner's agents together. The Stats "Users" tab lists owners but its rows lead nowhere.

An owner alias is a label an agent gave when registering (ADR 0012, ADR 0122). It is not an identity, so the operation has to show exactly which agents it matched before it changes anything.

## Decision

### 1. An owner page

The admin panel gets `#/owner?alias=<alias>`, a detail page like the agent page (the sidebar keeps **Agents** lit). It shows the alias with a note that it is self-reported, tiles (agents, active agents, memories written, verdicts given), and a table of the owner's agents: status, trust level, home fleet, entries and active, recent writes, last used, the successor if the agent was retired, and a link to its memories. The Stats "Users" rows, the agent page's "Owned by" line and the Agents table's Owner column link to it. An agent with no alias has no owner page.

The alias matches exactly as stored, capitals included. `Jane` and `jane` are two owners, as Stats already counts them.

### 2. One route, preview first

`POST /v1/admin/owners/{alias}/offboard` takes `{mode, successor, dry_run}`. `dry_run` is required, like retire. Admin key only, checked before anything is looked up.

- `mode: "retire"` needs a `successor`. Every active or revoked agent of the owner that is not already retired is retired to it, exactly as `POST /v1/admin/agents/{name}/retire` does (ADR 0113): a succession row, the agent revoked, its memories owned by the successor. A pending agent is rejected, as the revoke route does.
- `mode: "revoke"` takes no successor. Every active agent is revoked and every pending one rejected.
- An agent that is already revoked (revoke mode) or retired and revoked is skipped and listed as skipped, with the reason. An agent with a standing succession that is *active* again (an older pod can activate one, ADR 0113) still has a working key, so it is revoked in both modes under its existing succession, with no new one.
- The successor must be active and must not belong to the same owner alias (409 `succession_refused`: it would hand the leaving person's memories to the leaving person). The cycle and unknown-agent refusals of retire apply. A cycle for any one agent refuses the whole request.
- No agent has the alias: 404 `unknown_owner`. A body that does not fit (`retire` without a successor, `revoke` with one, even an unknown one) is 422, checked before anything is looked up.
- Applying (`dry_run: false`) must carry `expected_agents`, the `{name, action}` list the preview showed (422 without). When the plan under the locks is not exactly that, nothing is applied and the answer is 409 `offboard_changed`; the panel then shows the new preview and says the agents changed. This is the guard `expected_pin_ids` is for merges: nothing is changed unseen.

The answer is `{owner, mode, successor, agents: [{name, action, was, reason, ...}], dry_run}`. `action` is `retire`, `revoke`, `reject` or `skip`. A preview adds, for each retirement, the counts the retire preview gives (private notes, fleet memories, fleet memories in other fleets, pins). A preview changes nothing.

### 3. One transaction

Applying runs in one store transaction, `Store.offboard_owner`. It locks the owner's agent rows and the successor's in name order (`FOR UPDATE`), plans from the states it sees under the locks with one pure rule shared with the preview (`plan_offboarding`), and applies every step. The plan is compared with `expected_agents` under those locks, so an agent whose state changed since the preview refuses the request; a refusal, or an error part-way, leaves everything as it was. The key rows of every affected agent are then deleted, and only after that are the audit rows written. The status flip is what stops a key (ADR 0039).

### 4. Audit

No new action and no migration. Each agent gets the rows its own retire or revoke would: `agent.retire` and, for an active agent, `agent.revoke` with `via: "retire"`, or `agent.revoke` for a revoke or a rejection. Each row's detail also has `"offboarded_owner": alias`, so the audit log shows which rows belong to one offboarding. As everywhere (ADR 0027) the rows follow the change and are not part of its transaction.

### 5. The panel dialog

**Offboard this owner…** on the owner page opens a dialog. It offers retire to a successor (the default and the recommended one, because the memories keep an owner) or revoke all, the successor picker (active agents of other owners), the per-agent preview, and asks for the owner alias to be typed before it applies. It says that each retirement can still be undone one at a time, and that a revoke cannot be undone. The proxy allowlist gets the one route.

## Consequences

- One action replaces N dialogs, and the preview lists every agent the alias matches, including ones the admin forgot.
- Every retirement an offboarding makes is an ordinary succession: it shows in the Agents page's retired list and is undone one by one with the existing undo (ADR 0113). There is no "undo the offboarding".
- A revoke is not undoable, as today. An agent revoked this way can be activated again with a new key.
- Because an alias is a claim, anyone who registered under a leaver's alias is offboarded too. The preview is the check; the typed alias is the confirmation.
- An alias containing a slash cannot be addressed by the route (the path has one segment), so such an owner is handled agent by agent. Aliases are usernames or emails in practice.
- No migration, no new audit action, no change to ownership rules.
