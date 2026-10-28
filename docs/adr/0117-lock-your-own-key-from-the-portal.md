# Lock the agent's own key from the portal

Amends [ADR 0095](0095-agent-portal.md) (the portal cannot change anything but a withdrawal) and [ADR 0108](0108-replace-an-agent-key.md) (an agent cannot touch its own key). Touches [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md), [ADR 0027](0027-audit-log-of-admin-actions.md), [ADR 0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md) and [ADR 0042](0042-mcp-credential-is-reverified-per-call-and-fails-closed.md). Amends SPEC §5.1, §5.2, §12.3, §12.4 and §12.5.

Status: Accepted. Design from the 2026-10-28 session with Monochrome.

## Context

The person behind an agent can learn that its key leaked (it was pasted into a chat, committed, or left on a shared machine) long before an admin can act. Replacing the key (ADR 0108) is admin only, and ADR 0108 kept it out of the portal on purpose: it deletes the old key and issues a new one, and a thief holding the old key could use that to lock the owner out.

What the person needs in that gap is a way to stop the leaked key from working, now, without waiting for the admin, and without taking the agent itself down. Revoking the agent is too much: its memories, its pins, its trust level and its ratings would all change meaning. And locking a key cannot lock anyone out. A thief who presses the button only does to the key what the owner wanted anyway, and the admin hands the owner a new one.

Keys are checked on every request with no cache (`src/hivemind/store/auth.py`), so a mark on the key row takes effect on the next request everywhere.

## Decision

### 1. The portal gets "This key leaked"

`POST /v1/portal/key/lock` locks the key that made the request. Agent keys only (403 `forbidden` for the admin and org keys, as for every portal route). The portal shows it as a button behind a confirm step that says what happens: the key stops working on every surface, the person is signed out, the agent and its memories are untouched, and the admin has to issue a new key. After success the portal signs out and says what to do next: ask the admin for a new key.

The portal does not need a new kind of power. A lock only ever removes access, and anyone holding the key can press it. That is accepted: they could do worse with the key.

### 2. Only the key is locked

The agent stays `active`. Its memories, pins, trust level, home fleet and ratings are untouched, and its ratings keep counting. The lock is a nullable `credentials.locked_at`; nothing else changes. Because the agent is still active it keeps its name, and an admin can still change its level or fleet, clean up after it or retire it.

### 3. A locked key fails everywhere, with its own code

The check lives in the key lookup (`PgAuthenticator.verify`), which every surface calls, so there is nothing to forget. `verify` raises `KeyLocked` for the key of an active agent whose `locked_at` is set; for a pending, revoked or missing agent it still answers `None`, so a revoked agent's key is just unknown. Each surface maps `KeyLocked` to a clear answer:

- REST: 401 `key_locked`, message "this API key was locked because it may have leaked; ask the admin for a new key" (unknown or revoked keys keep `unknown_api_key`).
- MCP over HTTP: 401 `{"error": "key_locked", "detail": ...}` with the usual `WWW-Authenticate`.
- Stdio runner (`hivemind-mcp-pg`): the per-call check returns the tool error `key_locked` with the same message; at startup the runner exits with that message.
- Portal: the sign-in and every later call show the server's message.

The port gains no new method for this: `Authenticator.verify` may raise `KeyLocked`, and `Authenticator.lock_agent_key(name, key_id=...)` locks.

### 4. Locking is atomic and audited

`lock_agent_key` sets `locked_at` for the agent's key with the given fingerprint, only while it is unlocked, and writes the audit row in the same transaction. The fingerprint match means a press that arrives after an admin replaced the key locks nothing. A second press, or a press on an already locked key, changes and audits nothing (a locked key cannot make the request in the first place; the case is a race). New audit action `agent_key.lock`, `actor_kind` `agent_key`, actor the agent's name, target the agent's name, detail `{key: <fingerprint>, via: "portal"}`. No raw key anywhere.

### 5. Replace key clears the lock

The admin's "Replace key" (ADR 0108, `POST /v1/admin/agents/{name}/key`, `hivemind-keys rotate-agent`) deletes the key row and inserts a new one. The new row has no `locked_at`, so replacing is also how a locked agent gets back in. No code changes there; it keeps working on an `active` agent whose key is locked. Revoking a locked agent works as before and makes its key unknown again. Activating a revoked agent issues a fresh key, again without a lock.

### 6. The admin panel says which keys are locked

`GET /v1/admin/agents` carries `key_locked` and `key_locked_at` per agent. The Agents page shows a "key locked" badge next to the status and a filter for it, and the Replace key dialog says why the key is locked. `hivemind-keys list` marks a locked key. The idle-agents list is unchanged: a locked key is not used, so after the idle period the agent shows as idle too, and the badge tells the admin why.

## Migration

`0025.key-lock` adds `credentials.locked_at timestamptz` (nullable, no default, so no rewrite) and widens `audit_log_action_check` with `agent_key.lock`, the way 0017 to 0024 did. Expand-and-contract (ADR 0020): an older pod ignores the column, so during a rolling deploy it still accepts a key locked in that window. Do not rely on a lock until every pod runs this release. The rollback drops the column and deletes the `agent_key.lock` rows; a locked key becomes usable again, so an admin who wants it dead replaces it (ADR 0108) before rolling back.

## Consequences

- The person behind an agent can cut off a leaked key in seconds, and the agent's history is not touched.
- Until the admin replaces the key, the agent has no working key. The agent's harness sees `key_locked` and should say so; there is nothing it can do itself.
- The admin finds out from the Agents page (and the audit log), not from a notification.
- Anyone who holds a leaked key can lock it. They gain nothing from that.
