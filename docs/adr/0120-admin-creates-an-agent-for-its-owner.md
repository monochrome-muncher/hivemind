# An admin creates an agent for its owner

Touches [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md), [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md), [ADR 0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md) and [ADR 0046](0046-registrations-are-audited.md). Amends SPEC §5.1 and §12.3.

Status: Accepted. Asked for by Monochrome on 2026-11-07.

## Context

An agent gets its key in two steps: its owner registers it with the org key (`hive_register` or `POST /v1/agents`), and an admin activates it, which returns the key once (SPEC §12.3). Some owners never get round to the first step. The admin could already do both steps through the REST API, because `POST /v1/agents` takes the admin key, but not from the admin panel: its proxy forwards only `/v1/admin/*` routes and a few read-only ones, never the registration route. The CLI's `issue-agent` needs an agent that is already registered.

## Decision

### 1. One admin call

`POST /v1/admin/agents` with `{name, owner_alias, trust_level?, home_fleet_id}` registers the agent and activates it, and returns the agent record with its key, **once** (201). The trust level defaults to `lurker`, as on activation. It is admin key only: the org key and agent keys get 403, as on every `/v1/admin/*` route.

### 2. The same code path

`AccessService.create_agent` checks everything first (the admin key, the name rules of ADR 0040, a non-blank owner alias, a configured authenticator, a home fleet that exists and was not merged away) and only then runs `register` and `activate` unchanged. So the name rules, the case-insensitive uniqueness (ADR 0045), the key issue under the agent row's lock (ADR 0039) and the audit rows are the ones self-registration and activation already have: `agent.register` with `{owner_alias}` and `agent.activate` with `{trust_level, home_fleet_id}`, both with the admin key as the actor. There is no new audit action and no migration.

### 3. Only new names

A name that already exists, in any status, is a 409 `name_conflict`, and the existing agent is left alone. The message names the status, since the admin sees every agent anyway: a pending agent is activated, a revoked one re-activated, an active one gets **Replace key** (ADR 0108). This keeps the call from becoming a second way to take over a name someone registered. A case variant of an existing name is refused the same way.

### 4. The owner alias is required

On self-registration the alias is optional. Here the admin is creating the agent for someone, so the alias says who the key goes to and shows in the panel's owner grouping.

### 5. If activation fails

The two steps share no transaction, as before. If activation itself is refused after the registration (a fleet merged away in between, say), the agent stays `pending` with its owner alias and the admin activates it from the panel. If activation flips the agent to `active` and then the key issue or its audit row fails, the agent is `active` with no key the admin has seen, and the admin uses **Replace key** (ADR 0108). Either way it never ends up more privileged than intended.

A registration of the same name with the org key that lands between the check and the insert, by an owner alias that matches, is not told apart from the admin's own: both are written as one new registration, a second `agent.register` row is possible, and the admin's call then activates that record and gets its key. Two admins creating the same name at once end the same way, and the second gets 409 `invalid_status`. Only the admin ever receives the key, and activation still issues at most one, so this is accepted rather than adding an "inserted" flag to the store port.

### 6. The panel

The Agents page gets a **New agent** button. The dialog asks for the owner alias, the agent name, the trust level and the home fleet (with the same "create a new fleet" option as Activate) and shows the key in the copy-once dialog. The proxy allowlist gains `POST admin/agents`; `POST agents` stays out of it.

## Consequences

- An admin can register and activate an agent for its owner in one step and hand over the key directly.
- Self-registration with the org key is unchanged and stays the normal route.
- The agent portal does not change.
