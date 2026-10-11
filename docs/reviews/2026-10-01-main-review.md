# Hivemind security and performance review of main (2026-10-01)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Scope: `main` at 97988a7 (v2.0.2). Covers the REST API, both MCP runners, the admin panel, the store, search and history, deployment manifests and the plugin scripts. Every finding below was reproduced on Postgres 16 + pgvector 0.8.1 (or in-process) before it was fixed. The fixes are in PR #20: https://github.com/monochrome-muncher/hivemind/pull/20

The open follow-ups in ROADMAP §3.13 (rate limiting, registration audit, keyword semantics, bootstrap log keys, and the rest) were already known, so they are not repeated here.

## Findings, most severe first

### 1. Medium (DoS): `?history` loads an unbounded supersession tree. Fixed.
- `src/hivemind/services/chain.py:79-94` (main): the reverse walk returned every predecessor. Only the number of hops was capped (256), not the width.
- `src/hivemind/domain/validation.py:46`: `MAX_SUPERSEDES = 16`, so every write may supersede 16 entries. Any agent at L1 or above can legally build a tree of its own `self` entries (16 + 256 + 4096 ...), each with a body of up to 100 000 characters.
- Repro: a 273-entry tree gave 272 predecessors and a 26 MiB response per call. Ten concurrent calls peaked at about 300 MiB of Python heap. The mcp-http pod's memory limit is 512 Mi (`deploy/kubernetes/hivemind-mcp-deployment.yaml:171`). A depth-3 tree (about 4.4k writes) is about 430 MiB from a single call.
- Fix: one walk loads at most `MAX_LIMIT` (100) versions in both directions, and predecessors are fetched in batches. After the fix: 9 MiB per call and a 125 MiB peak for ten concurrent calls, whatever the tree size.

### 2. Low-Medium (DoS): the admin panel buffers keyless, unbounded bodies. Fixed.
- `src/hivemind/admin/app.py:135` (main): `content=await request.body()` ran with no key check and no size cap. The API's 2 MiB guard (`api/guards.py`) only runs after the panel has buffered the body.
- Repro: a 100 MiB POST with no key was forwarded in full, with a 202 MiB traced peak. The panel pod's limit is 192 Mi (`deploy/kubernetes/admin/hivemind-admin-deployment.yaml:97`). The impact is limited to whoever can reach the panel's port (ADR 0029 says internal ingress or port-forward only).
- Fix: the proxy applies the same rules as the API. A keyless call gets 401 (the health probe is exempt) and a body over 2 MiB gets 413, checked by Content-Length or by counting streamed bytes.

### 3. Performance: every entry read fetches and decodes the unused embedding. Fixed.
- `src/hivemind/store/pgstore.py:84-89` (main): `_ENTRY_COLUMNS` included `embedding`. `_row_to_entry` then converted it float by float on every get, list, search candidate fetch and history row. Nothing reads a stored vector, because search ranks in SQL.
- At dim 512, measured through `PgStore`: a 100-row list page went from 11.5 to 6.2 ms, and `get_entries(40)` (the search candidate fetch) from 4.1 to 2.3 ms. Each vector is about 4 KB on the wire per row at dim 1024 (the production default).
- Fix: reads leave the column out, and `create_entry` uses `INSERT … RETURNING`, which saves one round trip per write.

## Checked and not an issue
- MCP streamable-HTTP body size: the MCP SDK (2.2.0) already caps bodies at 4 MiB (`RequestBodyLimitMiddleware`). A 50 MiB POST with the org key got a 413.
- The visibility SQL (`pgstore._visibility_clause`) matches `domain.access.entry_is_visible`. Reads by id, feedback, withdraw and supersede all go through readability.
- The org key and level-0 keys read nothing and cannot write. Admin-only routes are gated in `AccessService`.
- The admin UI has no HTML sinks, and the proxy allowlist re-quotes path segments.
- The list, vector-search and history queries use their indexes (EXPLAIN on a 100k-entry pool: list 0.16 ms, vector search 10 ms).

## Observation (no change)
- On a 100k-entry pool where one term matched every row, the keyword stream took 357 ms, because `ts_rank` has to sort every match (`pgstore.py`, `search_keyword`). The data was synthetic and worst-case. This belongs with the deferred ROADMAP §4.1 (BM25 / FTS) item, not with a fix here.
