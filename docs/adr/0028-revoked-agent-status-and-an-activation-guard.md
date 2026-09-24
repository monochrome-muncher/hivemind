# A `revoked` agent status, and activation only from `pending` or `revoked`

## Context

ADR 0012 gave an agent two statuses, `pending` and `active`, and made
revocation a matter of deleting the agent's credential row. The status
did not move. Two consequences surfaced once an admin panel was
designed on top of `GET /v1/admin/agents`:

1. **A revoked agent is indistinguishable from an active one.** The
   listing shows `status = active` for both, and `AgentOut` carries no
   key state. A panel would render every revoked agent as active, with
   a Revoke button that visibly does nothing.
2. **`activate` does not check status.** `Store.activate_agent` updates
   the row whatever its status, and `Authenticator.issue_agent_key`
   inserts a new credential without removing any existing one. Calling
   `activate` on an active agent therefore issues a *second* valid key
   and leaves the first live. Nothing in the "one key per agent"
   glossary entry was enforcing it.

There was also no way to turn away an unwanted registration: a pending
agent could only be activated or left in the queue forever.

## Decision

1. **A third status, `revoked`.** The lifecycle is:

   | From | Verb | To |
   |---|---|---|
   | (none) | register | `pending` |
   | `pending` | activate | `active` (key issued) |
   | `pending` | revoke | `revoked` (a *rejected* registration; no key ever existed) |
   | `active` | revoke | `revoked` (key deleted) |
   | `revoked` | activate | `active` (a **fresh** key issued) |

   Every other transition is refused: `activate` on an `active` agent
   and `revoke` on a `revoked` one are **409** (`invalid_status`); an
   unknown name is **404**. Re-registering a `revoked` name is a
   conflict, exactly like an `active` one: names are durable
   (ADR 0012), and revocation does not release a name.

2. **Reject is revoke from `pending`.** There is no separate verb and
   no new audit action. The `agent.revoke` row's `detail` records the
   status it came from (`{"from": "pending"}` or `{"from": "active"}`),
   so a rejection is still distinguishable in the log.

3. **Status and credential move together.** `revoke` deletes the
   credential and sets `revoked`; `activate` sets `active` and issues
   the key. On the app path they are two port calls, ordered so that a
   failure in between leaves the agent *less* privileged, never more:
   revoke deletes the key first, activate flips the status first and
   issues the key last. The `hivemind-keys revoke` CLI command sets
   `revoked` in the same transaction as the credential delete.

4. **Migration `0006.agent-revoked-status`** widens
   `agents_status_check` to include `revoked` and backfills: an agent
   that is `active` but holds no agent credential is a pre-0006
   revocation, so it becomes `revoked`. Its rollback maps `revoked`
   back to `active` (the pre-0006 representation of the same fact: an
   active agent with no key) before restoring the narrow CHECK, so it
   destroys nothing.

5. **The audit log gains a `before` cursor.** `GET
   /v1/admin/audit-log?before=<id>` returns rows strictly older than
   the given row (by `occurred_at`, then `id`), so a reader can page
   back past the newest 1000 rows. This rides along here because the
   admin panel (ADR 0029) is its first consumer; it adds no schema.

## Consequences

* `hivemind-keys issue-agent` stays the break-glass path and does not
  consult status, but it now refuses an agent that already holds a key
  (the same double-key hole) and flips a `revoked` agent back to
  `active`, so status and key never disagree after a CLI run.
* A pre-0006 sibling pod (ADR 0020 expand-and-contract) never writes
  `revoked` and treats it as unknown on read. During a rolling
  deploy, an old pod listing agents could fail on a `revoked` row. The
  window is one rollout, the endpoint is admin-only, and it is not
  worth a two-step release.
* `GET /v1/metrics` gains nothing new; its per-status agent counts pick
  up `revoked` automatically if they group by status.
