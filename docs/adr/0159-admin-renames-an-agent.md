# An admin renames an agent

Amends [ADR 0040](0040-input-bounds-and-agent-name-rules.md) and [ADR 0045](0045-agent-names-are-unique-ignoring-case.md) (a name an agent left stays reserved), [ADR 0122](0122-admin-corrects-an-agents-owner-alias.md) (the sibling correction, whose shape this follows) and [ADR 0123](0123-agent-history.md) (the history follows former names). Touches [ADR 0027](0027-audit-log-of-admin-actions.md), [ADR 0109](0109-rename-a-fleet.md) and [ADR 0119](0119-rollbacks-do-not-silently-erase-the-audit-log.md). Amends SPEC §5.1 and §12.5.

Status: Accepted. Asked for by Monochrome on 2026-11-17. Decision 8 amended by [ADR 0160](0160-ratings-are-guarded-against-a-racing-rename.md): ratings take the same guard as entries and pins.

## Context

An agent's name could not be changed anywhere; only its owner alias could (ADR 0122). A name chosen in a hurry, a team renaming its agents, or a typo stayed for the life of the agent.

The name is the identity. `agents.name` is the primary key, and the name is copied as plain text, with no foreign key, into the key (`credentials.user_id`, `agent_id`, `agent_name`), into the memories the agent wrote (`entries.author`), into its ratings (`feedbacks."user"`), its pins, the successions it takes part in, its cleanups and the fleet merges that moved it. Ownership checks compare `author` with the caller's name (`owns`, and the `self` scope in `visibility_clause`), and the rule against rating one's own memory (ADRs 0065, 0067) compares the rater with the author. A rename that touches only `agents.name` would leave the agent unable to read its own `self` notes, free to rate its own memories and without a working key.

## Decision

### 1. Surfaces: the admin, over REST and the `hivemind-keys` CLI

`POST /v1/admin/agents/{name}/rename` with `{"name": "<new>"}`, admin key only (403 before the route looks at the agent, so a non-admin learns nothing about which names exist), answering the renamed agent as `PATCH /v1/admin/agents/{name}` does (`AgentWithRoleOut`). The panel's proxy allows the route. `hivemind-keys rename-agent OLD NEW` does the same from the break-glass tool, audited as a `cli` row like its other commands. There is no portal button and no MCP tool: an agent does not rename itself, because its name is what the memories, the pins and the history of its owner's work are filed under.

It works in any status (pending, active, revoked, retired): the name is a label on a record, not an access control, so there is nothing to protect by refusing a status.

### 2. The new name is checked as a registration's name

The same validation as registration: the bounds, hidden characters and reserved names of ADR 0040 (422 `invalid_input`), and unique ignoring case (ADR 0045). Another agent that holds the name in any case is 409 `name_conflict`, the code registration already uses, with the same message that says nothing more. An unknown agent is 404. A change of case alone (`bob` to `Bob`) is allowed and is a real rename. The name the agent already has answers 200 and writes no audit row, as the fleet rename does (ADR 0109).

### 3. Former names are reserved

The audit log and the history keep the old names, so a new agent must never take one: it would inherit, in every reader's eyes, the old agent's past. Migration `0036` adds `agent_former_names (name, agent, renamed_at)` with a unique index on `lower(name)`. `agent` is the agent's current name and is rewritten on every later rename, so the former names of an agent are the rows where `agent` is its name.

- Renaming `old` to `new` records `old` as a former name of `new` and moves the rows that named `old` to `new`.
- Registration (`POST /v1/agents`, `hive_register`, `POST /v1/admin/agents`) never inserts a name that is reserved, and refuses a former name in any case with the same 409 `name_conflict` as for an existing name. The store answers a registration of a former name as it answers a case variant of a current one: with the agent that holds it, whose name differs from the one asked for, which the service reports as taken. `hivemind-keys issue-agent` creates no agents (it refuses an unregistered name), so it needs no change.
- Renaming to a former name of another agent is 409. Renaming back to one of the agent's own former names is allowed and removes that row: the name is current again.
- A case-only change (`bob` to `Bob`) reserves nothing, because the agent still holds the name in another case; the reservation, unique on `lower(name)`, is made by the rename that leaves it. A reservation that already exists for the folded name (two legacy case variants, ADR 0045, both renamed) stands; the insert tolerates it.
- Former names are never released: there is no command to free one. A name is a part of the audit trail.

### 4. What is rewritten, in one transaction

One transaction, with the new name's registration lock (so a concurrent registration of the name waits), the old name's cleanup lock (so a cleanup of the agent is not running half on each name) and the agent row `FOR UPDATE`, rewrites:

- `agents.name`;
- `credentials.user_id`, `agent_id` and `agent_name` of the agent's key (`kind = 'agent'`), which `store/keys.py` writes as the same value. The key secret does not change, so the agent's harness keeps working and needs no new key;
- `entries.author`, and `entries.agent` of those entries where it equals the old name;
- `feedbacks."user"`, and `feedbacks.agent` where it equals the old name;
- `pins.pinned_by`; `successions.retired_agent` and `successions.successor`; `agent_cleanups.agent` and `cleaned_by` (a self-service cleanup records the agent as `cleaned_by`); `fleet_merges.moved_agents`;
- the `pinned_by` inside the pins an undoable merge (`fleet_merges.dropped_pins`) or cleanup (`agent_cleanups.unpinned_pins`) recorded, so an undo puts them back under the new name.

`successions.by` and `fleet_merges.merged_by` hold the acting admin (`admin:<fingerprint>` or the CLI actor), never an agent name, so they are left alone.

`entries.agent` and `feedbacks.agent` are the key's agent id when the key wrote them (`credential.agent_id or payload.agent`), which for an agent key is its name, and they are used as identity: the feedback pair `(user, agent)` identifies a reporter (ADR 0138), and `agent` is a search filter and the key of the per-agent feedback figures. So they follow the name where they hold it. Where they hold anything else (the label a plain key reported about itself, `claude-code`), it is not the identity and stays as written.

The cost is real. The rename updates every memory the agent wrote, and `author` is an indexed column, so each update is a non-HOT one: a new tuple and a new index entry in every index of `entries`, the HNSW vector index and the GIN keyword index included. The `feedbacks."user"` rewrite has no index to use and scans the table. A prolific agent therefore makes it far slower than the other corrections and it creates dead tuples and index bloat for autovacuum. It runs on the admin's request, in one transaction, like a fleet merge: without the server statement timeout, with a lock timeout of 5 seconds (client side `LONG_OP_TIMEOUT`), and a lock timeout or a deadlock (with a merge or move, which lock entries before agents) answers the same retryable `fleet_busy`, not a 500. While it runs, the agent's own writes wait for its row and answer `fleet_busy` (8).

Nothing is cached by name: the key check reads `credentials` joined to `agents` on every request, the access service holds no per-agent state, and the metrics and activity caches (figures by agent name) are dropped after a rename, as they are after an owner correction. They are dropped only in the process that served the request: other replicas, and a rename run with the CLI, serve the old name in those figures until their TTL (`metrics_entry_counts_ttl`) ends.

### 5. What is not rewritten

- The audit log is append-only (ADR 0027): rows written before keep the old name in their `target`, `actor` and `detail`. The `agent.rename` row is the record that they are the same agent, and the history follows it (6).
- The text of memories, notes and reasons that mention the old name, and the owner alias, which is not a name.
- A request already in flight under the old name. See 8.

### 6. Audit, and the history

New action `agent.rename`: the actor is the admin key (or `cli`), the target is the **new** name, the detail `{"from": old, "to": new}`. Migration `0036` widens `audit_log_action_check`; its rollback drops `agent_former_names` and deletes those rows, guarded like every audit-deleting rollback (ADR 0119). The action is in `AGENT_TARGET_ACTIONS`, so the history picks it up by target, and in the audit page's action filter.

The agent history (ADR 0123), used by the admin's agent page (`GET /v1/admin/audit-log?subject=`) and the portal (`GET /v1/portal/history`), matches rows by name. Both now add the agent's former names, and those of the agents it inherits from, to the names they ask for, so the rows written before the rename still show; the portal calls a former name "this agent" and shows a rename as "Renamed from X to Y". Succession is stored by name and rewritten with it, so the inheritance walk and the `inherits_from` of the key keep working.

### 7. Both stores

`Store.rename_agent` and `Store.former_agent_names` are on the agent port; `PgStore` and `MemoryStore` behave the same and the store contract tests run both. `rename_agent` returns the agent before and after, read under the row lock, so the audit row reports the name that was replaced even when two admins act at once.

### 8. A write racing the rename

A request authenticated under the old name that inserts after the rename commits would leave a memory, or a pin, with the old name. The cheap guard: the entry insert and the pin insert key-share-lock the author's agent row, which the rename takes `FOR UPDATE`. A write that started first makes the rename wait for it (and the rename then rewrites that row too); a write that starts after finds no agent of that name, sees that the name is a former one and is refused as a retryable `fleet_busy` (503, "retry"; the agent never supplies its name, so the message does not name it), the answer a write gets when its home fleet moved under it (ADR 0112). A write that has to wait for the rename's row lock gives up after 3 seconds with the same answer. The check is folded into the read the home-fleet check already makes (`FOR KEY SHARE`), and the lockers of agent rows that never change a name (status, level, fleet, role and owner changes, retirements, offboarding, merges, the key commands) take `FOR NO KEY UPDATE`, so ordinary writes do not wait behind them. The agent's next request authenticates under the new name. Authors that are not agents (the admin key) are not agents rows and are unaffected.

What is not guarded: a rating, a withdrawal or a cleanup running at the very moment of the commit can still file one row under the old name. The window is the length of the rename's transaction. The row is not lost (a withdrawal is audited, a rating stays on its memory), but nothing repairs it: a rating filed under the old name no longer counts as the agent's own for the own-entry rule, and an admin sees it in the feedback log under a name that no longer exists. It is not worth a lock on every read and write.

### 9. Panel

The agent buttons (the Agents list, the pending queue and the agent page share them) get **Rename** next to **Edit owner**, for every status: a small dialog with the current name filled in and a toast on success. On the agent's own page the panel then moves to the page of the new name; elsewhere it refreshes.

## Consequences

- A typo in a name, or a reorganisation, is fixed in one step; the agent does not notice except that `hive_whoami` shows the new name on its next call. Its key, trust level, fleet, memories, ratings, pins and what it inherited stay.
- The old name can never be registered again. A team that renames `bob` to `robert` cannot later give `bob` to somebody else; that is the point.
- Rolling back past `0036` needs `--discard-audit-rows` if any `agent.rename` row exists. The agents keep their new names and the reservations go.
- An older pod during a rollout does not serve the route, and does not know the reservation: a registration it serves could take a former name until every pod runs this release. Renames should wait for the rollout to finish.
- Tools that join the audit log to agents by name should use the former names (`agent_former_names`) or the `agent.rename` rows.
