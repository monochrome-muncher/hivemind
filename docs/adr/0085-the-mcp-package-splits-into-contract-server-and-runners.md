# The MCP package splits into contract, server assembly, and runners

## Context

`src/hivemind/mcp/server.py` (569 lines) held five roles at once: the agent
contract text (the server `instructions` + ten tool descriptions), the MCP
server assembly (`compact_schema`, the compact-schema server subclass,
`build_server`), the ten tool closures that redeclared each app function's
signature, and the two stdio runner entry points (`hivemind-mcp`,
`hivemind-mcp-pg`) with their key-hint texts. Changing any one role meant
opening a file where the other four lived; the contract text was invisible
outside the file; and the runner entry points were bound to the assembly
file in `pyproject.toml`.

## Decision

The package splits by role, in three green steps:

1. **`mcp/contract.py`** — the agent contract text: `INSTRUCTIONS` and the
   ten `DESC_*` tool descriptions. One home for the text the agents read;
   the agent plugin's drift test (`tests/unit/test_agent_plugin.py`) pins
   it in step and scans this module for the shared byte budget.
2. **`mcp/runners.py`** — the stdio runners: `main` (the in-memory dev
   server), `main_pg` (one process per agent, ADR 0009), and the
   key-hint texts. The console scripts point there
   (`hivemind-mcp = "hivemind.mcp.runners:main"`,
   `hivemind-mcp-pg = "hivemind.mcp.runners:main_pg"`).
3. **`mcp/server.py`** — the server assembly only: `compact_schema`, the
   compact-schema server, and `build_server`. `build_server` no longer
   redeclares each app function's signature in a private closure; a small
   `register_tool(server, name, description, verb, dispatch)` helper
   binds the verb, carries its signature (minus the bound `app`
   parameter) for the MCP schema, and routes every call through the
   per-call app resolution. The ten registrations are one explicit line
   each, referencing the contract's descriptions.

`mcp/app.py` is untouched by this split; it keeps the tool functions and
the `McpHivemind` composition (the wire mappers it used to own moved to
`hivemind.wire` in ADR 0084).

## Consequences

- The contract text has a named home; a sentence change touches
  `contract.py` and the drift test, nothing else.
- `server.py` is 179 lines and does one thing: it builds the
  `MCPServer`. The ten-closure block is a ten-line registration list.
- The runners move with their entry points; `pyproject.toml` names
  `runners.py`, and a runner change no longer opens the assembly file.
- `register_tool` is the seam between the MCP surface and the app
  functions: a new tool is one app function in `app.py` plus one
  registration line; the signature, the description reference, and the
  per-call app resolution are all handled by the helper.
