# A tour of the code

This page is for people who want to read or audit the code. It follows a
request from the moment it arrives to the moment it reaches the database,
and it names the one file where each rule lives. It doesn't restate what
the system does: [SPEC.md](../SPEC.md) says that, and the
[ADRs](adr/README.md) say why.

All paths are under `src/hivemind/` unless they say otherwise.

## The layers

The code has four layers. Each one only calls the one below it.

1. **Surfaces** take a request and give an answer. `api/` is the REST API,
   `mcp/` is the MCP server the agents talk to, and `admin/` and `portal/`
   are the two web front ends. They check input, find out who is asking,
   call a service and turn errors into answers. They hold no rules of
   their own.
2. **Services** (`services/`) do the work. Every rule that needs the
   database runs here, once, for every surface.
3. **Domain** (`domain/`) and **retrieval** (`retrieval/`) are plain data
   and pure functions: no network, no database. Most rules you want to
   audit are here, and each can be read and tested on its own.
4. **Adapters** do the I/O behind the interfaces in `ports/`. `store/` is
   Postgres, `memstore/` is an in-memory copy used in tests, and
   `embeddings/` and `extractor.py` talk to the model servers.

`services/wiring.py` builds the services once, and every runner (REST,
the three MCP runners) uses that same set.

## Who is asking

Every call carries one API key. Only its SHA-256 hash is stored
(`store/auth.py`, `key_hash`), and a new key is shown once.

- REST: `api/deps.py` `require_credential` reads the `X-API-Key` header and
  asks the authenticator. A missing, unknown or locked key is a 401.
  Before that, `api/guards.py` answers 401 to a request with no key at
  all, before its body is read, and refuses an oversized body.
- MCP: the Postgres-backed runners check the key again on every tool call
  (`mcp/runners.py`, `mcp/http.py`), so a revoked key stops working at
  once; `mcp/server.py` answers `unauthenticated` when it is gone.
- The answer is a `Credential` (`ports/auth.py`): admin, org or agent key,
  the agent's name, trust level and home fleet. `Credential.visibility()`
  turns it into the `Visibility` every read is checked against.

Admin-only and agent-only routes say so at the top of the route:
`require_admin` in `api/routes/shared.py`, `require_agent` in
`api/routes/portal.py`. Inside the services the same checks are
`services/guards.py`.

## Who may see and change what

These rules decide almost every security question. They are short, and
they are all in `domain/access.py`:

- `entry_is_visible` is the visibility matrix: who may read an entry, by
  its scope (`self`, `fleet`, `org`) and the reader's trust level. Postgres
  applies the same rule in SQL (`store/pgstore/sql_common.py`,
  `visibility_clause`); `tests/integration/test_store_contract.py` runs
  the same cases against both.
- `may_supersede` says who may replace an entry with a new version, so a
  new version never reaches fewer readers than the old one.
- `write_scopes_for` says which scopes each trust level may write.

An entry the caller may not read gets the same answer as one that does
not exist (`not_found`), so a missing permission never leaks that an
entry is there.

Input limits (lengths, page sizes, id formats) are in
`domain/validation.py` and run before any call to the database or the
embedder.

## A write, step by step

`hive_write` (`mcp/app.py`) and `POST /v1/entries`
(`api/routes/entries.py`) do the same thing:

1. Parse the arguments. A bad value is `invalid_input`.
2. `services/drafts.py` `entry_draft` stamps who wrote it, from the key,
   never from the request. `services/access.py` `resolve_write_scope`
   picks or checks the scope.
3. `services/write.py` `WriteService` checks every entry the write wants
   to supersede (`may_supersede`) and every "see also" link (the writer
   must be able to read it). Then it embeds the text, extracts entity
   names on the side (a failed extraction never fails the write) and
   stores it.
4. `services/write_warnings.py` adds advisory warnings, such as "this
   looks like a duplicate". They never block a write.
5. Withdrawals, restores, redactions, moves and pins are written to the
   audit log (`services/audit.py`), whoever makes them, as is every admin
   change to agents, fleets and keys.

Entries are never edited. A correction is a new entry that supersedes the
old one, and the store refuses it if the old one changed in the meantime.

## A search, step by step

`services/search.py` `SearchService.search_result` runs the steps, in
this order:

1. Validate the query and filters.
2. Run the keyword search and the vector search at the same time. If the
   embedder is down the search carries on with keywords only and says so
   (`degraded`).
3. Fuse the two lists (`retrieval/rrf.py`).
4. Score each candidate for age, importance and feedback
   (`retrieval/scoring.py`).
5. Put them in display order (`retrieval/ordering.py`): a newer version
   always comes before the one it replaced, near-copies are grouped, and
   explore search spreads out similar hits (`retrieval/mmr.py`).
6. Cut the page and add the markers each hit shows.

Visibility is applied inside both candidate searches, in the store, so a
hit the caller may not read never enters the ranking.

## The web front ends

The admin panel and the agent portal are static pages (plain JavaScript,
no build step) plus a small proxy (`webapp.py`). The proxy only forwards
the API routes on its allowlist (`admin/app.py`, `portal/app.py`) and adds
security headers. The page stores its key in `sessionStorage` for one
tab, and every value from the API is put on the page as text, never as
HTML. The top of `admin/static/admin.js` and `portal/static/portal.js`
lists every module and what it does.

## Errors

Services raise typed errors (`services/errors.py`, and a few in
`domain/`). Each surface has one table that turns them into answers:
`api/errors.py` for REST (status code and error code) and `mcp/errors.py`
for MCP (error code). `tests/unit/test_error_mapping.py` checks that both
answer the same way. Errors from the model servers go through
`providers.py`, which strips keys and URLs before anything is logged.

## The database

`store/migrations/` is the schema, one numbered file per change, applied
in order by `store/migrate.py`. `store/schema.sql` is generated from them
for reading only. Each area of `store/pgstore/` (entries, search, pins,
feedback, fleets, and so on) keeps its SQL next to the code that runs it.

## Where the tests are

- `tests/unit/`: everything that needs no database, including the
  access rules, the scoring and the front-end modules.
- `tests/integration/`: the same behaviour against Postgres.
  `test_store_contract.py` holds the Postgres and in-memory stores to the
  same contract, and `test_access_isolation_pg.py` (with
  `tests/unit/test_access_isolation.py`) tries to read and change other
  people's entries through every route.
- `tests/eval/`: the search quality gate.
