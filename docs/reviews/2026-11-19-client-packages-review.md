# Review of the `hivemind-memory` client packages (2.31.0)

Date: 2026-11-19. Baseline: `main` at f2a633d (v2.31.0). Scope: `clients/python`, `clients/typescript`, `clients/README.md`, `clients/live_server.py`, `.github/workflows/publish-clients.yml` and ADR 0162.

## How it was checked

- Both packages' type checks and unit tests (Python 3.10 with mypy strict, Node 22 with tsc): clean, 41 + 15 tests pass.
- The real HTTP MCP runner (`clients/live_server.py`) on a local Postgres, with both live tests: pass.
- The **published** 2.31.0 packages installed into a clean Python 3.10 venv and a clean npm project, then every README snippet run against the local runner: connect, write, search, rate, `run_tool` with good and bad input, a wrong key, a locked key, an unreachable URL, the LangChain tools, and the AI SDK adapter driven by the AI SDK's own mock model.
- The wheel, the sdist and the npm tarball unpacked and read.

The design holds up. Both packages are small, readable and close to each other; the async API, the error envelope as text for models, and the "one key is one agent" rule all work as the ADR describes. The findings below are the gaps.

## Findings

### Publishing

**P1. The next tag-triggered npm publish will fail.** `clients/typescript/package.json` has no `repository` field. Trusted publishing always attaches provenance (and `publish-clients.yml:95` asks for it with `--provenance`), and the npm registry rejects a provenance bundle when `repository.url` does not match the GitHub repository the workflow ran in (E422). The first publish was by hand, without provenance, so this has not shown yet. Fix: add `repository` (with `directory`), `homepage`, `bugs` and `keywords`. `clients/python/pyproject.toml` likewise has no `[project.urls]`, so the PyPI page links nowhere.

**P2. The build runs with the publishing token in reach** (`publish-clients.yml:22-50`, `52-95`). Each job holds `id-token: write` while it installs and runs third-party code: `uv build` fetches an unpinned `hatchling` (line 44), and `npm ci` (line 90) runs the install scripts of the whole dev tree, including `ai` and its dependencies; `npm publish` then runs `prepublishOnly` (`package.json:30`). A compromised build dependency could mint the OIDC token and publish a tampered `hivemind-memory`. The PyPA guidance is to build in a job without `id-token` and publish the built files from a second job that runs nothing else. Fix: split each registry into build and publish jobs, `npm ci --ignore-scripts`, publish the packed tarball.

### Errors

**E1. A locked key is reported as an unknown one.** The HTTP runner answers a locked key with HTTP 401 and `{"error": "key_locked", "detail": ...}` (`src/hivemind/mcp/http.py:78-91`, `130-131`). Both clients turn every 401 into `AuthenticationError("unauthenticated", "the server rejected the API key (HTTP 401)")` (`clients/python/src/hivemind_memory/client.py:103-111`, `clients/typescript/src/client.ts:78-80`), so `.code == "key_locked"`, which both READMEs and ADR 0162 promise, never happens, and the server's message on how to replace the key is lost. Checked live: locking the live agent's key gives `unauthenticated` in both. The unit tests miss it because both fakes send `key_locked` as a tool reply, which the real server never does.

**E2. The two packages handle odd replies differently.**
- Python `run_tool` returns the SDK's raw text when a tool fails outside the envelope (for example a schema validation error: `Error executing tool hive_write: 2 validation errors ...`), while TypeScript wraps it as `{"error": {"code": "tool_error", ...}}`. The Python README promises the envelope (`README.md:68`).
- TypeScript reads only the first content block (`client.ts:95`); Python joins every text block.
- TypeScript returns a JSON reply that is not an object as if it were one (`client.ts:120`); Python raises `invalid_reply`.
- Both return a failed call (`isError`) whose text is JSON but not the envelope as a success.
- Python raises `AuthenticationError` for a 401 at any point in a session; TypeScript only during `connect()` (`client.ts:76-81`).

### Docs, read by a person

**D1. The first Python example does not run as a script.** `README.md:26` uses `async with` at the top level; pasted into a file it is a syntax error. The first example should be a whole program with `asyncio.run`.

**D2. `await using` does not run on Node 22.** The TypeScript README leads with `await using hive = await Hivemind.connect()` (`README.md:20`, `40`). Node 22 cannot parse it (`SyntaxError: Unexpected identifier 'hive'`, checked on 22.23.5); it works when TypeScript compiles it, or on Node 24. The README should say so, since Node 22 is the stated floor.

**D3. The TypeScript loop is not complete.** `task` is never defined (`README.md:42`), and the model id is `claude-sonnet-4-5` (`README.md:45`) while the Python README uses `claude-sonnet-5-5`.

**D4. The reminder is described, not shown.** Both READMEs explain in prose how to send the reminder once and run one more turn. A person, and even more an agent copying the loop, gets this right more often from code. The loops should include it.

**D5. TypeScript has no Development section** (how to run the tests and the live test), unlike Python.

### Docs and API, used by an agent

**A1. The AI SDK example stops after one step.** `generateText` in AI SDK 7 defaults to `stopWhen: isStepCount(1)`. With the README's example (`README.md:82-90`) the model calls a tool, the call runs, and generation ends with empty `text`; the model never sees the result. Checked with the AI SDK's mock model: one step and `""` by default, two steps and the answer with `stopWhen: stepCountIs(10)`. The example also uses `hive` and `model` without defining them.

**A2. Python's typed methods take plain `str` for closed sets.** `kind`, `verdict` and `recency` are `str` in Python but unions in TypeScript (`types.ts:6-8`). An agent writing code against the Python package cannot see the valid values from the signature or the IDE. `Literal` types give it the same help TypeScript has.

**A3. Tool details point to the whole spec.** Both READMEs send the reader to `SPEC.md` or `hive.tools` for argument and reply details. The useful target is SPEC §5.2, and `hive.tools` (the server's own descriptions) deserves first place, because that is what the model sees.

**A4. The LangChain tools are async only.** `tool.invoke(...)` raises `NotImplementedError: StructuredTool does not support sync invocation`. Expected for an async package, but the README should say to use `ainvoke` or an async agent.

**A5. Network and HTTP failures are not described in the Python README.** TypeScript says they are ordinary errors from the MCP SDK; Python says nothing (an unreachable URL raises `httpx2.ConnectError`, a 503 from a busy store an HTTP error from the SDK).

## Left as they are

- The packages ship `tests/` in the sdist; harmless, and useful to downstream packagers.
- The wheel holds exactly the package, `py.typed` and the two texts; the npm tarball holds `dist`, `text`, `README.md`, `LICENSE` and `package.json`. Nothing extra.
- `workflow_dispatch` cannot publish from a branch: both jobs require `github.ref_type == 'tag'` (`publish-clients.yml:22`, `53`).
- Python's `instructions()` ends with a newline, TypeScript's does not. No effect.

## Open questions for the owner (design, not fixed here)

1. **Typed replies.** Every method returns `dict[str, Any]` / `Record<string, unknown>`, so an agent writing `entry["id"]` or `written.id as string` gets no help. Typed reply shapes for the common tools (search hit, entry, whoami) would help agents most, at the cost of tying the packages to the reply contract.
2. **Plugin-only lines in the shipped skill.** The skill copy mentions `hivemind-setup`, the plugin and `hivemind-incognito <harness>`, which a custom agent has none of. ADR 0162 chose a byte-identical copy; a short client note, or trimming those lines at sync time, would make `instructions()` fit an app better.

## Fixes

| Finding | Pull request |
|---|---|
| P1, P2 | Fix and harden client publishing |
| E1, E2 | Report a locked key and align error handling |
| D1-D5, A1-A5 | Runnable client quickstarts and typed options |
