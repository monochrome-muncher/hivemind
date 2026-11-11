# Review of the changes since 2.19.0

Read against v2.20.0 (merge commit 98fcac1), compared with v2.19.0 and v2.19.1. It covers #357 to #370, with most of the weight on the readability refactor (#362 to #370), which was meant to change no behaviour.

**Result: no behaviour change and no bug found.** The only problems were in comments: 13 comments that cite the wrong ADR, say something the code does not do, or dropped a fact found nowhere else. They are fixed in one comment-only pull request.

## How it was checked

| Check | Result |
|---|---|
| OpenAPI document, v2.19.1 against v2.20.0 | Identical except `FeedbackOut.may_supersede` (#357, intended) |
| MCP `tools/list`, v2.19.1 against v2.20.0 | Identical |
| Comment PRs #367, #369, #370: syntax tree of every changed file with docstrings set aside | Identical in all files: no code changed |
| Moves #362 to #365: every function and method before the split, matched by syntax tree to one after it (leading underscores ignored) | All match. The only differences are renamed helpers (`_copy` to `copy_entry`, `_row_to_entry` to `row_to_entry`) and the route handlers, which now take the app through `AppDep` / `CredentialDep` instead of closure aliases |
| `PgStore` and `MemoryStore` composition | No method defined twice along either MRO, and none left to the `Store` protocol |
| Module-level state after the split | None duplicated; the store logger keeps the name `hivemind.store.pgstore` |
| Tests that monkeypatch module constants (`VECTOR_SEARCH_SETTINGS`, `PREVIEW_*_LIMIT`) | Each patches the module that reads the name, so the tests still test something |
| #366 named steps (search ranking, activity report, write warnings, cleanup audit and revoke, keys CLI, web app proxy, MCP replies and runner) | Read in full against the old code: same calls in the same order, same messages, same exit codes. The proxy routes keep their names (`index`, `healthz`, `proxy`) |
| #368 store steps (cleanup, undo, merge, move, unmerge, entry insert, `relied_on_changed` clause) | Postgres methods read in full: same statements, arguments, timeouts and lock order. In-memory `fleet_health` and `activity` read in full; in-memory merge and move left to the store contract tests |
| #357 supersede hint | Correct: the hint uses the same `may_supersede` rule and default write scope as the write path, and never fires for `helpful`, the admin key or legacy keys |
| #358 to #361 | Config, docs and tests only; nothing that runs in production |

## ADR references in the shortened comments

The earlier write-up trusted the ADR numbers in the compressed comments and only spot-checked them. This time all 758 rewritten hunks of #369 and #370 were checked: does the cited ADR say what the comment points it at, and did the old comment hold a fact that is now nowhere else (not in the code, the ADR or SPEC.md)?

Almost all were fine. What was not:

| Where (main) | Problem | Fix |
|---|---|---|
| `domain/history.py:263` | Cites ADR 0124 for "the reason is shown only for this agent's memory"; 0124 only says that only an admin restores | Says both, and that it matches redact |
| `domain/access.py:90` `FleetBusy` | Says it only means a lock wait, but `pgstore/fleet_guards.py:48` and `memstore/state.py:141` also raise it when the author's home fleet changed during the write | Names both causes |
| `admin/app.py:57` | The fleet rename route sits under "Fleet merge and move" | Rename and ADR 0109 added |
| `api/routes/__init__.py:1` | Cites ADR 0001 (append-only entries) for "routes stay thin"; that rule is in AGENTS.md | Citation dropped |
| `config.py:100` | Cites ADR 0024 (unknown env var names) for range checks on search settings | Citation dropped |
| `store/kube_secret.py:51` | Dropped that `enableServiceLinks: false`, which the bootstrap Job sets, does not remove the `KUBERNETES_SERVICE_*` variables | Restored |
| `store/pgstore/__init__.py:39` | Dropped why the pool opens on first use (synchronous builders hand the store to a running loop) | Restored |
| `mcp/http.py:158` `_MCP_PATH` | Dropped that `/mcp` is the SDK's own default route and is only a metrics label here | Restored |
| `mcp/app.py:75`, `mcp/http.py:2`, `mcp/runners.py:73`, `store/__init__.py:48`, `api/main.py:134`, `store/auth.py:1` | Cite ADR 0008, which is superseded by 0012 (the current key model). Most were there before the refactor, but now the ADR is the only explanation left | Point at ADR 0012 (and 0039 for the key kinds) |

ADR 0007 is still cited in a few places although ADR 0026 superseded it. That is deliberate: ADR 0007's status note keeps its citations for the clauses that still hold (one Postgres node).

## Not covered

- The admin and portal JavaScript, which the refactor did not touch.
- Speed: Thursday's weekly performance check covers the refactor.
