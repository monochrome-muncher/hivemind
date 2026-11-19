# The client packages type the tools' replies

Builds on [ADR 0162](0162-python-and-npm-client-packages.md) decision 2 (one method per tool).

Status: Accepted. Asked for on 2026-11-19, after the review of the client packages (`docs/reviews/2026-11-19-client-packages-review.md`).

## Context

Every method returned `dict[str, Any]` in Python and `Record<string, unknown>` in TypeScript. A developer, or an agent writing code against the package, got no help with `entry["id"]` or `written.id` and needed casts in TypeScript. The reply shapes are the server's wire contract (`src/hivemind/wire.py`, the tool replies in `src/hivemind/mcp/app.py`).

## Decision

1. Each method returns its reply's shape: `TypedDict`s in `hivemind_memory.replies`, interfaces in the npm package's types. Keys the server leaves out when they have nothing to say are optional. `get` and `feedback` return the batch shape for a list of ids, and `write` with `check` returns the check reply. `call()` stays untyped.
2. The types describe; they never validate. A reply is the parsed JSON, unchanged, so a key a newer server adds reaches the caller even before the types list it.
3. Each package's live test, which CI runs against the real HTTP runner, checks that the replies of `hive_whoami`, `hive_write` (and its check), `hive_search`, `hive_get`, `hive_list`, `hive_pinned` and `hive_withdraw` carry every required key and no undeclared one. In TypeScript the key lists are checked against the interfaces at compile time.

## Consequences

- A reply key the server adds, renames or drops fails the client live tests, so the types change with the server in the same pull request.
- The feedback, pin and register replies are typed but not checked live: the live agent can rate no entry of its own, pins need a pinnable fleet entry, and registering needs an org key.
