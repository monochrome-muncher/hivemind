# Refactoring review of the whole codebase (2026-11-26)

- **Main checked:** `d9b4438` (after #515, unreleased on top of v2.32.3).
- **Asked for:** a thorough look for refactoring opportunities across the server, admin panel and portal, plugins and skills, client packages, tests, deploy and dashboard. Duplication, overlong functions and modules, unclear names, leaky abstractions, dead code, inconsistent patterns and tests that are hard to maintain.
- **Not re-raised:** everything done or left alone on purpose in the 2026-11-01 refactor review, the 2026-11-10 and 2026-11-15 readability reviews, the 2026-11-12 refactors review (ADR 0149), the 2026-11-19 full review (#478, #479) and the 2.31.0..2.32.1 review. Also the owner's decisions: `pins.fleet_id` stays text, the hand-built `EntryFilters` are held together by the parity test (#216) rather than one shared mapping, the `MemoryStore`/`PgStore` twins are by design, `config.py`, `_summary` and `_usage_families` stay whole.
- **Method:** every Python function over 60 lines and every module over 400 lines listed; every builtin exception the services catch from a store; every guard call in the REST routes; every name defined twice across the admin and portal JavaScript; every test module imported by another test module; a `vulture` pass, each hit checked by hand. Line numbers point into `d9b4438`.

## Result

The code is in good shape. The layers hold, the store twins agree, and the long functions left are flat tables or SQL steps. What remains is mostly the same rule written in two or three places, a few seams that still leak, and test files that grew past the point where a person or an agent can find their way in them.

## Findings, ranked by value against risk

| # | Finding | Value | Risk | Reviewer |
|---|---|---|---|---|
| A | Write and feedback input rules split between MCP, REST and the domain | high | low-medium | yes (MCP contract text) |
| B | Entry store methods signal with bare `KeyError`/`ValueError` | medium | low | no |
| C | Admin and agent-key guards written by hand in 38 routes, missing in 15 | medium-high | medium | yes (auth) |
| D | Test builders live in test modules; tests wire `McpHivemind` by hand | medium | low | no |
| E | One 482-line migration rollback test, 97 copies of connect/try/finally | medium | low | no |
| F | Two test files past 3,000 lines | medium | low | no |
| G | Front-end helpers and dialog blocks copied between admin and portal | medium | low-medium | no |
| H | Middle-man services: forwarding methods and reach-through attributes | medium | low-medium | no |
| I | Credential SQL and the audit insert spelled out four times | low-medium | low | no |
| J | Pin replies built inline in MCP, outside the wire contract | low-medium | low | no |
| K | Small dead code | low | very low | no |

### A. Write and feedback input rules split between the surfaces (high value, low-medium risk)

#515 moved the bounds into the MCP schemas. The rules themselves are still checked in more than one place, or only at the surface:

- **Write scope vocabulary.** MCP checks `self|fleet|org` by hand (`mcp/app.py:345-349`), REST by a `Literal` (`api/schemas/entries.py:68`). The rule's owner, `resolve_write_scope` (`services/access.py:624`), gives an unknown scope rank 99 and so answers `PermissionDenied` (`:637`), the very "a typo is not a trust-level problem" case MCP guards against. `resolve_write_scope` is also pure, so it belongs in `domain/access.py` beside `write_scopes_for`.
- **Agent identity on a write.** Both surfaces check it before `entry_draft`, with the same message (`mcp/app.py:350-355`, `api/routes/entries.py:63-69`). `AgentIdentityRequired` already exists and both error tables map it (`mcp/errors.py:81`, `api/errors.py:82`). `entry_draft` (`services/drafts.py:23`) can take `agent: str | None` and raise it.
- **Blank source ref.** Checked in `_parse_sources` (`mcp/app.py:177-178`) and in `SourceModel` (`api/schemas/entries.py:39-44`), while `EntryDraft._validate_bounds` checks only its length (`domain/entry.py:188-189`). One line there covers both surfaces.
- **"Naive means UTC"** is written seven times: `api/schemas/common.py:21`, `api/routes/shared.py:48` (a second `to_utc`), `mcp/app.py:102-112`, `domain/entry.py:208`, `domain/validation.py:115`, `domain/fleet_phase.py:54`, `store/reextract.py:282`. One `as_utc` in the domain.
- **Filters validated twice on MCP.** `_build_filters` calls `filters.validate()` (`mcp/app.py:215`), then the service does it again (`services/read.py:154`, `services/search.py:309`). The docstring at `domain/entry.py:308` ("service-built filters skip it") no longer describes what the services do.
- **Feedback identity pre-check.** `_feedback_precheck` re-tests the reporter identity (`mcp/app.py:232-237`) that `FeedbackService._reporter` raises as `AgentIdentityRequired` (`services/feedback.py:121-126`); REST relies on the service alone.

**Fix:** the rules move into the domain and the services; the surfaces keep their schemas (for clients to see) and only parse. Error codes stay the same; a few MCP refusal texts change wording, so the reviewer checks the contract and the error-mapping tests. One PR.

### B. Entry store methods signal with bare builtins (medium, low)

Every other store area raises a typed error (`UnknownAgent(KeyError)`, `UnknownFleet(KeyError)`, `SupersedeConflict`, ... `domain/access.py:55-90`, `ports/entries.py:21`). The entry methods raise plain `KeyError` for "unknown entry" and `ValueError` for "not active" (`store/pgstore/entries.py:468-529`, `store/pgstore/self_move.py:63-75`, `memstore/entries.py:253-345`, `memstore/self_move.py:23-27`), and `GovernanceService` catches them as builtins (`services/governance.py:103`, `:142`, `:173`, `:206-209`). `except ValueError` there also catches `InvalidInput` (a `ValueError`, `domain/validation.py:16`) or a row mapper's enum parse, and would report either as `EntryNotActive` (409) instead of a 422 or a 500. AGENTS.md asks for typed errors only around service calls.

**Fix:** `UnknownEntry(KeyError)` and `EntryNotActiveConflict(ValueError)` in `ports/entries.py` next to `SupersedeConflict`, raised by both stores, caught by name. Subclassing the builtins keeps any caller that still catches them working.

### C. Route guards written by hand (medium-high, medium)

- 23 of the 38 `/v1/admin/*` routes call `require_admin(credential, "...")` by hand (`api/routes/shared.py:55`); the other 15 rely on the service's guard (for example `admin_agents.py:37-93`, `admin_fleets.py:29-85`, `admin_audit.py:31-75`). Because FastAPI parses the body before the route function runs, a non-admin key with a malformed body gets 422 on those 15 and 403 on the rest.
- All 15 portal routes call `require_agent(credential, "...")` by hand, 16 calls (`api/routes/portal.py:67`).

**Fix:** an `AdminCredentialDep` and an `AgentKeyCredentialDep` set as the router's dependencies on the admin and portal routers, so the check is declared once, always runs before the body is parsed, and cannot be forgotten on a new route. A unit test asserts that every `/v1/admin/` and `/v1/portal/` route carries it. The service guards stay (defence in depth, and the MCP surface). Messages become one per router. Auth change, so it gets a reviewer.

### D. Test builders live in test modules (medium, low)

- 18 test modules import builders from `tests/unit/test_api_endpoints.py` (`make_hivemind_app`, `post_entry`, `make_authenticator`, `WRITER`), and others from `test_mcp_app.py`, `test_readable_feedback.py`, `test_mcp_http.py`, `test_successions.py`, `test_prometheus.py`. A test file doubles as a fixture library, so moving or splitting it breaks unrelated tests.
- 15 test files build `McpHivemind(...)` with every service by hand (for example `tests/unit/test_see_also.py:39-48`) instead of `McpHivemind.from_services(store, build_services(...), credential)`, so a new service means editing 15 files.
- `McpHivemind.store` and `McpHivemind.search_config` (`mcp/app.py:69`, `:75`) are set but never read in `mcp/`; they exist only because the hand wiring passes them.

**Fix:** the shared builders move to `tests/builders.py` (next to `tests/fakes.py`), with one `mcp_app(store, credential, **overrides)` builder; drop the two unused fields. Pure test move plus a two-field removal.

### E. The migration rollback test (medium, low)

`tests/integration/test_migration.py:258` `test_rollback_removes_the_latest_migration` is 482 lines: it peels 26 migrations one at a time, each with its own seed rows and checks, and a 40-line docstring lists every step's purpose apart from the step itself. The file has 97 `asyncpg.connect(dsn)` / `try` / `finally: close()` blocks.

**Fix:** an async `_connected(dsn)` context manager for the whole file, and the rollback test as a table of steps, `(migration id, seed, check)`, run in order by one loop (the order and the seeded rows stay, so the upgrade path coverage the 2026-11-15 test review kept stays too). Each step's sentence moves from the docstring onto its step. Same tests, same order.

### F. Two test files past 3,000 lines (medium, low)

- `tests/integration/test_store_contract.py`: 5,639 lines, 232 tests in 25 area sections (`# -- pins (ADR 0058)` at `:1623`, `# -- fleet merge` at `:3561`, ...) that mirror the `ports/` modules. It becomes a `tests/integration/store_contract/` package: the `Adapter` fixture and draft helpers (`:104-196`) in its `conftest.py`, one module per area.
- `tests/unit/test_agent_plugin.py`: 3,172 lines with per-harness sections (incognito `:667`, `hivemind-as` `:874`, OpenCode `:1169`, Cline `:1532`, KiloCode `:1839`, DeepSeek Harness `:2427`, reminder texts `:2737`, Antigravity `:3092`). It splits the same way. The budget history in comments (`:469-494`, eight measurements over a month) is changelog; the ADRs hold it, so it shrinks to the current figure.

An agent fixing one area then reads 300 lines, not 5,600. Pure move: the collected test ids are compared before and after; AGENTS.md and the code tour are updated where they name the file.

### G. Front-end helpers copied (medium, low-medium)

- `plural` is defined six times with two signatures (`admin/static/cleanup.js:15`, `merge.js:194`, `explain.js:6`, `retire.js:10`, `offboard.js:7`, `portal/static/cleanup.js:17`); `pad` and `localValue` three times (`admin/static/cleanup.js:12-14`, `portal/static/cleanup.js:14-16`, `audit.js:98`).
- The cleanup preview blocks (`entryRow`, `withdrawBlock`, `ratingsBlock`, `restoreBlock`) exist in both cleanup modules (`admin/static/cleanup.js:17-66`, `portal/static/cleanup.js:19-68`), 111 identical lines; only the hint texts differ.
- The "type the name to confirm" field is built in seven dialogs (`admin/static/cleanup.js:80`, `:144`, `merge.js:106`, `:177`, `:212`, `retire.js:82`, `offboard.js:100`, `drawer.js:289`, `:301`, `portal/static/cleanup.js:86`). `offboard.js:100` compares without `trim()`, unlike the others, so a trailing space refuses there only.
- The sign-in shell (`showLogin`, `showShell`, the `signOut` core) is the same in `admin/static/admin.js:110-131` and `portal/static/portal.js:85-108`.

**Fix:** `plural`, `localValue` and `typedConfirm(expected)` into `ui.js`; a shared `cleanupblocks.js` with the hint texts passed in, served to the portal through `SHARED_MODULES` (`portal/app.py:24`); optionally a small `shell.js`. The node-run front-end tests and `test_admin_panel.py`'s module map cover it.

### H. Middle-man services (medium, low-medium)

- `GovernanceService` has ten methods that only forward to `FeedbackService` or `PinService` (`services/governance.py:211-283`).
- `AccessService` creates five sub-services and exposes them as attributes (`services/access.py:115-119`: `merges`, `successions`, `offboarding`, `history`, `cleanups`); routes reach through it 16 times (`app.access_service.merges.preview(...)`, `admin_fleets.py:106`) and so does the composition root (`services/wiring.py:63-65`).

**Fix:** `Services` (`services/wiring.py:21`) carries `feedback_service`, `pin_service`, `merge_service`, `succession_service`, `offboarding_service`, `cleanup_service` and `history_service` directly; the surfaces call them; `GovernanceService` keeps withdraw, move to self, restore and redact. The one thing to keep is the `require_stored_keys` downgrade on feedback and pins (`wiring.py:46-52`), which then sits on those two services' construction. A wide but mechanical diff; AGENTS.md's service list is updated with it.

### I. Credential SQL spelled out four times (low-medium, low)

`DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1` appears four times under three names (`store/auth.py:74` and `:88` in the same module, `store/keys.py:61` and `:78`); the audit insert four times (`store/pgstore/audit.py:16`, `store/auth.py:75`, `store/keys.py:67`, `store/migrate.py:95`); the agent-status row lock twice (`store/auth.py:73`, `store/keys.py:72`). One home for the credential statements and one `insert_audit(conn, ...)`. SQL text byte-identical, checked by dumping the constants before and after.

Not recommended: routing the `hivemind-keys` CLI through `AccessService`. The CLI activates and issues the key in one transaction (`store/keys.py:199-233`) and audits the OS user; the REST path is two steps. Different contracts, and the CLI is the break-glass tool.

### J. Pin replies outside the wire contract (low-medium, low)

`hive_pinned` builds each pin row inline (`mcp/app.py:673-687`), and `hive_pin` and `hive_feedback` their rows too (`:734-741`). Their REST twins are `PinnedOut` and `PinOut` (`api/schemas/pins.py:39-66`). `tests/unit/test_wire_contract.py` pins entries, hits, links, feedback summaries, the fleet phase and `whoami`, not pins. **Fix:** `pinned_dict` and `pin_dict` in `wire.py` and a contract case each.

### K. Small dead code (low, very low)

- ~~`retrieval/scoring.py:134` `entry_score` has no production caller~~. Correction while fixing: it has no production caller, but 53 test and eval call sites use it (`test_ranking_invariants.py` pins its signature as the ranking-input allowlist, and the `tests/eval/` harnesses score with it). It stays as the tests' entry point to the formula.
- The two `McpHivemind` fields in D.

Folds into D.

## Looked at and left alone

- **Long functions:** `hive_write` (100 lines) is mostly its docstring and shrinks with A; `hive_search` and `hive_list` likewise. `pgstore/fleet_merge.py` `move_agents` (79) and the memstore cleanup methods (69-70) read as steps. `_health_report` (82) is a flat assembly.
- **Long JS dialogs:** `merge.js` `moveDialog` (114 lines) and `drawer.js` `render` (111) are cohesive; revisit after G shrinks them.
- **`memstore/entries.py:87-105`:** the supersede-denial predicate is inline; naming it `_may_flip` would read slightly better. Too small for its own PR; could ride along with B.
- **Deploy:** the four Deployments repeat the security context; plain manifests are easier for an operator to read, and `test_deploy_manifests.py` keeps them consistent. Kustomize components would hide that. The dashboard JSON is hand-kept and tested against the emitted series.
- **Plugins and skills:** the per-harness copies are required by the harnesses and pinned by `test_agent_plugin.py`; nothing to fold.
- **Client packages:** reviewed on 2026-11-19; the reply types are hand-kept on purpose (ADR 0162) and checked by their own tests.
- **Test-only port methods** (`count_entries`, `get_fleet_merge`, `issue_admin_key`, `WriteService.write`): kept as test seams, as the 2026-11-19 review said.

## Suggested order

A and B first (most value, both touch the write path and governance; B before H). Then D, E and F (tests only, no risk to behaviour, and F makes the later PRs easier to review). C with a reviewer. G, H, I, J when convenient. Each is its own PR.
