# Full review of v2.30.0 (2026-11-19)

- **Main checked:** `2eeac4f` (v2.30.0, PR #477).
- **Asked for:** a thorough review of the whole project: bugs, security, performance, refactors and readability, tests, docs and README drift, plugins, skills and harness docs, migrations, deploy and metrics.
- **Baselines not repeated:** the 2026-11-15 major review, readability, test-suite and plugins reviews; the 2026-11-16 security audit (`3f6e65a`, clean); the 2026-11-17 docs and harness check (every harness installed live); the 2026-11-19 perf check (`ed07fb7`) and #476. Decisions the owner made, the dropped list and the rejected Caura ideas were not re-raised.
- **Baseline checks:** `make check` clean. Unit suite green. Main's CI (unit + Postgres + Docker) is green at `2eeac4f`; the local Postgres stage could not be rebuilt in this container (the session hook's `apt-get update` hung), so the integration suite was left to CI on each PR.

## Result

No security issue and no correctness bug in the server. The code since the security baseline (agent rename and its rating guard, the `unverified` tag, the tool-call `code` label, the Cursor manifest, the admin Edit dialog) was read in full and holds up. What turned up is small: documentation drift (fixed in #478) and two dead helpers (removed in #479). A few info notes are below; none needs work now.

| PR | Kind | What |
|---|---|---|
| [#478](https://github.com/monochrome-muncher/hivemind/pull/478) | docs (low/medium) | Drift: Antigravity skills "a link" (medium), OpenCode/KiloCode reminder events, `hivemind-as` Claude profile rule, CHANGELOG `other` code value, runbook MCP tool-call series, incognito usage `code-insiders`, three deploy comments citing superseded ADR 0013 |
| [#479](https://github.com/monochrome-muncher/hivemind/pull/479) | refactor | Remove `EntryDraft.supersedes_ids` (no caller) and `Visibility.entry_visible` (only tested itself) |

## Security

Read line by line, beyond the 11-16 audit:

- **Agent rename (ADR 0159, `store/pgstore/agent_rename.py`).** Every table that stores an agent name as text was listed from `schema.sql` and checked against `REWRITES`: `agents`, `credentials` (agent keys only), `entries.author` (and `agent` when it equalled the name), `feedbacks."user"`, `pins`, `successions` (both sides), `agent_cleanups` (agent, cleaned_by, the pins JSON), `fleet_merges` (moved agents, dropped pins JSON). The audit log stays append-only; the history and audit filters add the former names (`services/audit.py:65`, `services/agent_history.py:40`). The new name is validated and reserved names refused before the store call (`services/access.py:380`). A name an agent left cannot be registered: `INSERT_AGENT` re-checks `agent_former_names` inside the insert (`pgstore/agents.py:38-43`), under the same advisory lock as registration.
- **Write and rating guard (ADRs 0159, 0160, `pgstore/fleet_guards.py:44`).** Writes key-share-lock the author's row; the rename takes `FOR UPDATE`, every other locker was moved to `FOR NO KEY UPDATE` (`store/auth.py:73`, `store/keys.py:72`, `pgstore/agents.py:21`), so only a rename conflicts. A rater or author that is not an agent (`admin`, `org`, `dev`) is reserved (`domain/validation.py:60`) and so never trips the folded "was renamed" check.
- **Credentials after a rename:** both MCP runners resolve the key on every call (`mcp/runners.py:112`, `mcp/http.py:126`), so the new name applies at once. No in-process cache is keyed by agent name except the activity and metrics caches, which the rename hook clears (`services/wiring.py:62`).
- **Front ends:** no HTML sinks (`innerHTML` and friends) in either UI; entry sources render as text (`admin/static/drawer.js:192`); the CSP is strict (`webapp.py:46`); CSV export guards formula cells (`admin/static/csv.js:15`).
- **Deploy:** non-root, read-only root filesystem, no service account token, dropped capabilities in every Deployment and Job; the key bootstrap Role can only create Secrets and get/delete `hivemind-keys` (`deploy/kubernetes/bootstrap/rbac.yaml`).
- **Metrics label:** `code` takes values only from `ERROR_CODES` plus `none`, `raised`, `other` (`mcp/metrics.py:28-34`), so its cardinality is bounded.

## Performance

Nothing to fix. Info:

1. **Every rating is now a transaction with a lock check** (`pgstore/feedback.py:231-232`, ADR 0160). Before 2.29.0 a single upsert; now `BEGIN`, `SET LOCAL lock_timeout`, the key-share read of the rater's row (plus, only when the rater is not found, the renamed check), the upsert and `COMMIT`. A few extra round trips on `hive_feedback`, each on a primary key. Accepted in ADR 0160; worth remembering if `hive_feedback` latency on the dashboard rises.
2. **Many concurrent writes by one agent** now share-lock the same `agents` row, which Postgres records as a multixact. Harmless at the write rates Grafana shows; noted for completeness.
3. The weekly perf check's notes stand (fleet health scan, rename scanning `feedbacks`, ADR 0126 scans).

## Bugs and robustness

None found. Checked: the rename's case-only change and own-former-name paths in both stores, the rating guard for non-agent raters, the `unverified` filter's SQL against `domain.feedback.still_unverified` and its memstore twin, the admin Edit dialog's partial-failure path (`admin/static/agents.js:218-247`), the write service's supersede and see-also resolution, and the 0036 rollback (it follows the 0029-0035 pattern).

## Refactors and readability

- Done: #479.
- Test-only port methods (info, not changed): `count_entries`, `get_fleet_merge` and `Authenticator.issue_admin_key` have no production caller but are used widely by the contract tests as assertions; `WriteService.write` is used only by tests. Keeping them is reasonable while they serve as test seams.
- The rest of the code reads cleanly; the readability reviews of 11-10 and 11-15 hold.

## Tests

- No contract divergence is marked `xfail` in either contract suite, so `MemoryStore` and `PgStore` agree everywhere they are tested.
- The front-end tests that need `node` skip without it; GitHub's Ubuntu runners ship node, so they run in CI. Nothing to change.

## Migrations

Head `0036.agent-rename`; checksums for all 69 files are pinned; the head and chain in `tests/integration/test_migration.py` match. 0036 adds a table and widens the audit CHECK (expand only).

## Deploy, metrics and dashboard

- Manifests, probes, grace periods and the NetworkPolicy examples are consistent with DEPLOY.md and the runbook. Three comments cited superseded ADR 0013 (fixed in #478).
- Every dashboard and alert query names a series the code emits. The tool-call panels split failures from refusals as ADR 0161 says.
- Info: the `hivemind_health_fleet_unverified` gauge (ADR 0156) has no dashboard panel; the admin panel's vital signs show it, as the ADR intends. Add a panel only if the owner wants it in Grafana.
- Info: `server.json` gives the registry a default embedding model of `Qwen/Qwen3-Embedding-0.6B`, while the code default is `text-embedding-3-small` (`config.py:261`). The registry value is a sensible suggestion for the image (it matches the 1024 default width); left as is.

## Docs and plugins

- Core docs (README, SPEC, AGENTS, CONTEXT, DEPLOY, runbook, code tour, ROADMAP): every REST route (68) and MCP tool (10) matches SPEC; all 57 settings are in `.env.example` with matching defaults; every path in AGENTS.md and the code tour exists; the ADR index has one row per ADR (160) with matching statuses; Makefile targets and entry points named in docs exist; versions agree in all thirteen files.
- Plugin: the fourteen harnesses are listed consistently; skills match the tool names and `hive_whoami` fields; the Antigravity skills copy is identical; the always-on skill is 9,588 B. Drift fixed in #478.
- Left alone: the Hermes incognito check (`bin/hivemind-incognito:174`) only looks for the string `HIVEMIND_ENABLED` in `config.yaml` before printing a note; a stricter match would add little, since it only decides whether to print advice.

## Harnesses

Not re-run: the 2026-11-17 check installed all fourteen live two days ago, and the Tuesday routine re-checks upstream releases.

## Method

- Code since the security baseline (`3f6e65a..2eeac4f`, 70 files in `src/`) read in full; older code spot-checked by bug class (name copies after a rename, lock modes, HTML sinks, formula cells, caches keyed by name).
- `vulture` dead-code scan of `src/`, each hit checked by hand.
- Two read-only scopers checked the core and plugin docs; every finding acted on was re-checked by hand.
