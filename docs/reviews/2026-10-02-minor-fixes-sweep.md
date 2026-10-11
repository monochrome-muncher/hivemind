# Hivemind minor-fixes sweep (2026-10-02)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Scope: `main` at 5097f4f (v2.3.3). Read the services (write, feedback, pins, governance, chain, search, access, metrics), the REST routes, schemas and guards, the MCP tools and runners (stdio, pg, HTTP), PgStore and PgAuthenticator, the admin proxy, the domain validation, config env allowlist, the extractor and the plugin shell scripts. Findings from the 2026-10-02 full review and architecture review are not repeated.

Short answer: no security issue and nothing urgent. Three small bugs, each reproduced against the local Postgres and fixed in its own PR. Everything else checked out.

## Fixed (one PR each)

### 1. PR #82: an id spelled `{uuid}`, `urn:uuid:...` or with extra hyphens gave a 500
- `src/hivemind/store/pgstore.py` `_is_valid_uuid` used `uuid.UUID`, which accepts these spellings. asyncpg's uuid codec rejects them: `DataError: invalid UUID ... length must be between 32..36 characters`. Nothing maps `DataError`, so the caller got a 500.
- Reachable by any authenticated caller: `GET /v1/entries/{id}`, `hive_get`, withdraw, feedback, pin and unpin.
- Fix: accept only what the codec accepts (hex digits and hyphens, 32 to 36 characters). Such an id now answers 404 / `not_found`. A new contract case runs on both stores.

### 2. PR #83: an audit-log `since` that overflows in UTC gave a 500
- `GET /v1/admin/audit-log?since=0001-01-01T00:00:00+14:00` is year 0 in UTC. asyncpg raises `DataError: date value out of range` (`src/hivemind/api/routes.py`, audit_log route).
- Admin-only, so this is input hygiene, not security. Fix: 422 `invalid_input`. Far-future values like 2999 keep working. `check_datetime` (1900..2200) was not reused, because the existing test relies on 2999.

### 3. PR #84: supersedes by an upper-case or hyphen-less id was refused
- `src/hivemind/services/write.py` `_check_supersedes` looked targets up by the id as given, but `PgStore.get_entries` keys by canonical id. The write answered `supersede_denied` although the store's own flip accepts these spellings (an existing test pins that) and `see_also` already works in any case.
- Fix: match targets with case and hyphens folded. The impact was small because agents copy canonical ids from replies.

## Suggestions only (no PR)

- **Incognito launcher, Pi branch** (`plugins/hivemind/bin/hivemind-incognito`): if `~/.config/mcp/mcp.json` or `.mcp.json` holds a JSON value whose root is not an object, the inline Python raises `AttributeError`. Under `set -eu` the launcher then exits before starting Pi. Catching `AttributeError` alongside `OSError, ValueError` would fix it. This is an edge case on malformed user config. Fixed later in PR #85 (2.3.4): a malformed config file is skipped.
- **Withdrawing a successor** leaves its predecessors `superseded`, so neither version shows in search any more. This looks deliberate (entries are immutable; withdraw means retract), but it is a design question, not a minor fix.

## Checked, no issue
- **Prometheus request labels.** `method` comes from the client, but the shipped image uses httptools (`uvicorn[standard]`), which rejects unknown methods with a 400 before the app sees them. Verified with a raw socket: label cardinality stays bounded.
- **Env allowlist.** Every `HIVEMIND_*` name used in `src`, `entrypoint.sh`, `deploy` and `config` is a `Settings` field or in `_ALLOWED_EXTRA_ENV_VARS`.
- **Admin proxy.** The allowlist, dot-segment refusal, segment re-quoting and key-before-body check all hold.
- **MCP HTTP auth.** The credential is set per request and reset after it, the provider fails closed, and a store timeout answers 503, not 401.
- **Visibility.** Feedback, pins, links and history all go through `get_visible_entry` / `get_visible_entries` first. The batch paths use canonical ids.
- **Naive datetimes** are UTC on every REST and MCP filter and on `occurred_at`. Search and list filters are bounded by `check_datetime`.

Baseline: the full suite was green on main (with `HM_TEST_REQUIRE_PG=1`) before the changes. Each PR passed `make check`, and the unit and integration tests passed locally.
