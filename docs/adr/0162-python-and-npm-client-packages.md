# Python and npm packages for agents built on Hivemind: `hivemind-memory`

Builds on [ADR 0010](0010-hostable-multi-agent-streamable-http-mcp-runner-with-per-request-credentials.md) (the hostable MCP endpoint), [ADR 0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) (the agent contract: server instructions and tool descriptions) and [ADR 0161](0161-mcp-tool-calls-carry-their-error-code.md) (error codes on tool replies). Contrasts with [ADR 0150](0150-key-profiles-on-the-client.md) (key profiles), which it does not use.

Status: Accepted. Asked for on 2026-11-19, design agreed with the owner the same day. Decision 6 amended by [ADR 0163](0163-client-skill-copy-drops-plugin-only-lines.md): the skill copy drops its plugin-only lines.

## Context

The agent plugin serves fourteen harnesses. A developer who builds their own agent or system, on an agent framework or straight on a model API, has the `/mcp` endpoint and nothing else: no skill, no session routine, no end-of-task nudge, and no tool definitions in the shape their model API wants. Most agent frameworks already speak MCP, so the missing part is not transport but the contract around it.

The REST API under `/v1` is not that contract. It carries no tool descriptions, none of the notes agents read (loose match, degraded search), and much of it is admin and portal routes.

## Decision

1. One package per language, both named **`hivemind-memory`**: on PyPI (import `hivemind_memory`) and on npm. `hivemind` and `hivemind-sdk` are taken on PyPI, and `hivemind` on npm. They live in `clients/python/` and `clients/typescript/`.
2. They are **MCP clients of the hostable endpoint**, built on the official MCP SDK of each language, never REST clients. A client offers:
   - `connect()`, which opens the session and calls `hive_whoami`, raising a typed error for a rejected or locked key;
   - one async method per tool, with the tool's arguments as typed parameters;
   - `instructions()`: the server's `instructions` from the MCP handshake, followed by the `hivemind` skill;
   - `reminder()`: the end-of-task write-and-rate text the Stop hook sends;
   - the tool definitions as plain data for the Anthropic Messages and OpenAI function-calling formats, with no dependency on either SDK;
   - a typed error carrying the reply's error code.
3. **Async only**, in both languages.
4. **One adapter per language, as an optional extra**, for the most-used framework in November 2026: LangChain in Python (`hivemind-memory[langchain]`, whose tools LangGraph runs too) and the Vercel AI SDK on npm (`hivemind-memory/ai-sdk`, `ai` as an optional peer dependency). Each returns that framework's own tool objects and the instructions as a system message. Other frameworks connect to `/mcp` with their own MCP support and take `instructions()` from the package; the READMEs show how.
5. **Keys come from arguments or the environment**, `HIVEMIND_MCP_URL` and `HIVEMIND_API_KEY` as the plugin uses them; an argument wins. No key profile files: profiles are for a person switching harnesses, and an app runs where its operator puts its secrets. One client object is one agent; acting as another agent means a new client and a new conversation (the reason is ADR 0150 decision 3).
6. The skill and the reminder are **copies made at build time** from `plugins/hivemind/` (`make sync-client-text`), and a unit test keeps them byte-identical.
7. **Supported runtimes:** Python 3.10 and later; Node 22 and later (Node 20 is past its end of life, and the AI SDK needs 22), ESM only, written in TypeScript with its types shipped.
8. **Versions move in lockstep with the server.** `clients/python/pyproject.toml`, `clients/typescript/package.json` and its lockfile join the release's version files, and the plugin tests pin them.
9. **Publishing** runs on the `vX.Y.Z` tag push, like the image and the MCP Registry, through PyPI and npm trusted publishing (GitHub OIDC, no stored token). Nothing is published from a pull request.
10. **Tests:** unit tests in each language against a fake MCP server; one CI job starts the real HTTP runner on the Postgres service and runs a write, search, rate and withdraw from both packages. Neither joins the server's pytest suite.

## Considered and not done

- **A REST client.** It would duplicate a surface agents do not use and miss the contract text.
- **Adapter packages for several frameworks.** Each adds a dependency to track; frameworks with MCP support already work without one.
- **A sync API.** The MCP SDKs are async; `asyncio.run` covers a script.
- **Running the session routine in `connect()`** (pinned, recent, flagged). The skill tells the agent to do it, and the agent decides what bears on its task.

## Consequences

- The skill and reminder have one source; a change to either needs `make sync-client-text`, which the test enforces.
- A release bumps three more files, and its notes cover the packages.
- The first npm publish is done by hand: npm trusted publishing needs the package to exist.
