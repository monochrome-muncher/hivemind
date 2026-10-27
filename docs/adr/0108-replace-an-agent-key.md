# Replace an agent's key

Amends [ADR 0028](0028-revoked-agent-status-and-an-activation-guard.md) (a key is replaced only by revoking and re-activating) and touches [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md), [ADR 0027](0027-audit-log-of-admin-actions.md) and [ADR 0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md). Amends SPEC §5.1, §12.3, §12.4 and §12.5.

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

An agent's key leaks, or its owner loses it. Before this, the only fix was `hivemind-keys revoke --name X` and then `hivemind-keys issue-agent --name X --trust-level N --home-fleet ID`, from a shell. That flips the agent to `revoked` and back to `active`. The admin had to remember the agent's old level and fleet, because a revoked agent is never restored with stale values (ADR 0039). `issue-agent` on an active agent that still holds a key is refused (`AgentKeyExists`). The admin panel could not replace a key at all.

The schema already allows one agent key per agent (`credentials_one_agent_key`, migration 0007). Keys are checked on every request with no cache (`src/hivemind/store/auth.py`), so a deleted key row fails on the next request.

## Decision

### 1. One operation, admin only

`POST /v1/admin/agents/{name}/key` replaces an agent's key and returns the new raw key **once**, as activate does (`KeyIssuedOut`). The CLI gets `hivemind-keys rotate-agent --name X`, which prints the key the same way. The admin panel gets a "Replace key" button on an active agent, with a confirm step and the copy-once key dialog. The agent portal does not change: an agent cannot replace its own key, because a leaked key would then let a thief lock the owner out.

### 2. The old key dies at once

The key row is deleted and the new one inserted in one transaction, under the agent row's lock (`SELECT ... FOR UPDATE`, as issue and revoke take it, ADR 0039). The agent never holds zero or two key rows, and the old key fails on its next request. A racing revoke cannot leave a revoked agent holding a key. There is no grace period for the old key: the point of a replacement is usually a leak.

### 3. Only for active agents

An agent that is `pending` or `revoked` is refused with 409 `invalid_status` (404 for an unknown name). A revoked agent still goes through `issue-agent` or the panel's "Re-activate", which sets its level and fleet again. An active agent that somehow holds no key gets one. If one admin replaces the key while another activates the same pending or revoked agent, the two serialise on the agent row. If the replace runs second, it kills the key the activation just returned, and the second admin holds the live one. That is harmless: the agent ends with exactly one key, and the activation is not undone.

### 4. Nothing else changes

Name, status, trust level, home fleet, memories, ratings and pins stay as they are. Nothing changes for agents except that the old key stops working and they use the new one.

### 5. Audit

New action `agent_key.replace`, target the agent name, detail `{old_key, new_key}`: the two key fingerprints (the first 12 hex characters of the stored hash, as `hivemind-keys list` shows), `old_key` null if the agent held none. Never a raw key (ADR 0027). Through REST the actor is `admin:<fingerprint>`; through the CLI it is the `--actor` name with `actor_kind` `cli`. The audit row is written in the same transaction as the swap on both paths, so there is never a new key without its row, and a failed audit write rolls the swap back.

### 6. The port

`Authenticator.replace_agent_key(name)` returns a `KeyReplacement` (raw key, new fingerprint, old fingerprint). `AccessService.replace_agent_key` checks the admin key first (a non-admin learns nothing about the agent) and calls the port. `PgAuthenticator` does the swap and writes the audit row in the same transaction, under the actor the service passes (`admin:<fingerprint>`). The CLI does its own swap in the same transaction as its audit row, as the other CLI commands do.

### 7. Not logged

The response body is a raw key. The admin panel's proxy already marks every response `Cache-Control: no-store` and neither the proxy nor the API logs bodies; the new route is added to the proxy allowlist and to the comment that names the key-bearing routes.

## Migration

`0018.agent-key-replace` widens `audit_log_action_check` with `agent_key.replace`, the way 0017 did. It is additive (ADR 0020): an older pod never writes the action. The rollback deletes the `agent_key.replace` rows and restores the 0017 constraint. Replaced keys stay as they are.

## Consequences

- An admin can cut off a leaked agent key in one step, without remembering the agent's level and fleet.
- The agent is without a working key from the moment of replacement until its owner pastes the new one. That is the intent.
- Replacing a key does not tell the agent. The admin delivers the key out of band, as at activation (SPEC §12.3).
