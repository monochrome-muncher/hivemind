# Hivemind architecture and maintainability review (2026-10-02)

> A review kept for reference. Findings, line numbers and PR states are as of the commit it names, so the code may have moved on. [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

Scope: `main` at 436a3c0 (v2.3.2), all of `src/hivemind` (about 9 400 lines in 50 modules) and the test layout (1 359 tests). Read as a senior architect and engineer, against the contracts the repo already sets for itself: SPEC.md, the ADRs (0001-0060), AGENTS.md and its "Invariants that are easy to break". Security, performance and bugs were reviewed separately on 2026-10-01/02 and are clean; they are not repeated here.

## Verdict

The code is in a maintainable state, and a "major refactor" in the sense of a restructuring or rewrite is not warranted: it would put a lot of reviewed, ADR-backed behaviour at risk for little gain. The architecture is sound. What it has is a handful of local debts, mostly duplication at the edges and conventions that only discipline enforces. Those can be paid down with small, behaviour-preserving refactors, one PR each, proven by the existing suite plus characterization tests.

## What is good (keep it)

- **Clear layering.** A pure domain (`domain/`, no I/O), ports (`ports.py`), one orchestration layer (`services/`), and two thin surfaces (`api/`, `mcp/`). The dependency direction is respected: no service imports a surface, the domain imports nothing above it.
- **Single sources of truth where it matters.** The visibility matrix lives once in `domain/access.py:175` (`entry_is_visible`) and is mirrored in SQL in one place (`store/pgstore.py:369` `_visibility_clause`). Input bounds live once in `domain/validation.py` and run at `EntryDraft` construction, so REST and MCP reject the same inputs (ADR 0040).
- **Pure retrieval math** in `retrieval/` with hand-computed unit tests and an eval gate.
- **Strict tooling.** mypy strict + ruff clean, 1 359 tests, Postgres integration in CI with `HM_TEST_REQUIRE_PG=1`, migration checksums, backward-compat script.
- **Decisions are written down** (60 ADRs with an index and status notes) and the code cites them, so intent is recoverable.
- **SQL hygiene.** All statements are named constants with bind parameters; the only dynamic SQL builds placeholders (`$n`), never values.

## Debts, ranked by value for the risk

Each is behaviour-preserving unless marked otherwise.

### 1. The service graph is wired by hand in four places (and in ~10 test files)
`api/main.py:47-57`, `mcp/server.py:402-415`, `mcp/server.py:474-482` and `mcp/http.py:290-305` each build `WriteService`, `SearchService`, `GovernanceService` and `AccessService` from the same parts. The startup log line is copied three times (`api/main.py:87`, `mcp/server.py:457`, `mcp/http.py:323`). Adding a service or a constructor argument means editing every runner, and missing one is silent (the runner that was missed simply lacks the feature).
**Refactor:** one composition root, `services/wiring.py` `build_services(...)` returning a frozen `Services` bundle, used by every runner; one `log_startup(...)`. Public constructors (`create_app_for_config`, `build_http_app`, `McpHivemind`, `HivemindApp`) keep their signatures. Risk: low.

### 2. `PgStore` repeats the pool-acquire boilerplate 36 times
`store/pgstore.py` opens every method with `pool = await self._ensure_pool()` + `async with pool.acquire(timeout=self._acquire_timeout) as conn` (36 sites). A new method that forgets the timeout argument waits forever on a saturated pool instead of answering 503.
**Refactor:** a private `_connection()` async context manager (and `_transaction()`), so the acquire timeout is applied in one place. SQL and transaction boundaries unchanged. Risk: low.

### 3. MemoryStore/PgStore parity is a convention, not a test
AGENTS.md says `MemoryStore` "must behave like `PgStore`", but `tests/unit/test_memstore.py` and `tests/integration/test_pgstore*.py` are separate suites with different cases. The one known divergence (tie order in search, full review finding 7) went unnoticed for that reason. Every service test runs on `MemoryStore`, so a divergence makes those tests prove the wrong thing.
**Refactor (tests only):** a `Store` contract suite parametrized over both adapters (MemoryStore always; PgStore when the database is up, required in CI) covering the port's documented behaviour: create/supersede atomicity, filters, visibility per trust level, links, pins and their cap, feedback upsert and ordering, withdraw errors, agent lifecycle errors. Known divergences are pinned explicitly rather than hidden. Risk: none at runtime.

### 4. Errors are a protocol of builtin exceptions, decoded by position
Services signal outcomes with `LookupError`, `KeyError` and `ValueError`, and each surface decodes them with an ordered `except` chain (33 such clauses in `api/routes.py` and `mcp/app.py`). The same `ValueError` means 409 `conflict` in `withdraw` (`routes.py:382`), 422 `agent_identity_required` in feedback (`routes.py:480`), 409 `name_conflict` in register (`routes.py:516`) and `not_active` on MCP (`app.py:620`). `InvalidInput` subclasses `ValueError`, so every chain must list it first (`validation.py:19` says so). `PermissionDenied` lives in `services/governance.py:38`, so `services/access.py:36` depends on the governance module just for an exception class.
**Refactor:** a `services/errors.py` with typed errors (`NotFound`, `NotActive`, `AgentIdentityRequired`, ...) that subclass today's builtins, so every existing `except` keeps working, and `PermissionDenied`/`SupersedeDenied` moved there with re-exports from `governance`. Each surface then maps error type to (status, code) in one table. The codes and messages on the wire stay byte-identical, pinned by characterization tests per route and tool. Risk: medium (touches every error path), which is why it comes after the contract tests and needs its own characterization tests first.

### 5. The write path and filter parsing are duplicated across REST and MCP
`api/routes.py:169-205` and `mcp/app.py:343-378` both resolve the author, the agent, the write scope and the importance provenance (default 3 vs caller, ROADMAP §4.5) before building `EntryDraft`. `EntryFilters` is built three times (`routes.py:306`, `routes.py:341`, `mcp/app.py:266`). A rule change (say a new default) has to be made twice, and a drift would make REST and MCP disagree, which the SPEC forbids.
**Refactor:** one `services/drafts.py` helper for the credential-derived parts of a draft, called by both surfaces in the same check order each uses today (so a request with two errors still gets the same error first). Risk: low to medium.

### 6. `services/governance.py` mixes three services
It holds `WriteService` (embed, extract, supersede, see-also, related), `GovernanceService` withdraw + feedback + pins, and the shared exception classes (496 lines). The pin logic alone (`pin`, `unpin`, `pinned`, `_pins_shown_as`, `_current`, `_pinnable`) is a cohesive unit.
**Refactor (changes the module layout):** split into `services/write.py`, `services/feedback.py`, `services/pins.py`, keeping `governance.py` as a re-export shim so no import breaks. This changes where things live in AGENTS.md's architecture map, so it is a question for the owner first.

### 7. Small items
- `services/search.py:271-282`: `apply_supersession_invariant` finds each chain link with a linear `next(...)` scan, O(n²) over up to 2 x `candidate_top_k` candidates. A dict lookup is equivalent (ids are unique) and clearer.
- `mcp/app.py:245` `_check_pagination` and `mcp/app.py:284` `_supersession_chain` are one-line wrappers with no added meaning.
- Scope and state are compared as string literals in places (`services/governance.py:261` `entry.state.value != "active"`, `governance.py:486` `entry.scope != "fleet"`, `services/access.py:345` `_SCOPE_RANK`), although `SCOPE_*` constants and `EntryState` exist.
- `MetricsService.usage_report` returns `dict[str, dict[str, Any]]` (`services/metrics.py:43`), so the REST and Prometheus consumers index it by string keys with no type check.

## Not recommended

- **Making `scope` an enum on `Entry`.** It would touch the domain model, both stores, both surfaces' serialisers and the schema contract for little practical gain. Constants are enough.
- **Splitting `PgStore` into per-aggregate repositories.** At 1 200 lines it is long but flat and regular (SQL constants, methods, mappers). Splitting it would scatter the transaction boundaries ADR 0034 depends on.
- **A DI framework or an ORM.** No new dependencies; the explicit wiring above is enough.

## Plan and outcome

Behaviour-preserving PRs, one each, opened 2026-10-02:
1. #74 Composition root (debt 1): `services/wiring.py` `build_services` + `log_startup`.
2. #75 `PgStore` connection helper (debt 2): `_connection()` / `_transaction()`.
3. #76 Store contract suite (debt 3): 28 cases on both adapters. It found five MemoryStore/PgStore divergences, now strict xfails: keyword tie order, vector search dropping similarity <= 0 (memory), same-write link order, `quality_counts` for unknown ids (pg answers zeros), `list_agents` order. Aligning MemoryStore is a follow-up.
4. #77 Typed service errors (debt 4, reduced): `services/errors.py` (`EntryNotFound`, `EntryNotActive`, `AgentIdentityRequired`, plus `PermissionDenied`/`SupersedeDenied` moved out of governance) subclassing the builtins the surfaces catch, and 17 wire-level characterization tests. The surfaces' `except` chains were deliberately not narrowed (that would turn an unexpected `ValueError` into a 500); with the tests in place that can be a later, deliberate change.
5. #78 Shared draft building (debt 5): `services/drafts.py` `entry_draft`. Filter construction left as is (a dataclass call, not worth a helper).
6. #79 Small items (debt 7): dict lookup in `apply_supersession_invariant` (pinned to the old version on 300 random cases), MCP one-line wrappers removed. String-literal comparisons in governance left for after #77.

Open question for the owner: debt 6, the module split of `services/governance.py`.

Merge note: #74, #77 and #78 each add a sentence to the same AGENTS.md line and touch `mcp/app.py` imports, so the second and third to merge need main merged in.
