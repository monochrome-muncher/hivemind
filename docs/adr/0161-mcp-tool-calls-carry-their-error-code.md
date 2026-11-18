# MCP tool calls carry their error code

Amends [ADR 0104](0104-mcp-runner-serves-process-metrics.md) decision 2 (the tool-call counter's labels).

Status: Accepted. Asked for on 2026-11-18, after production Grafana showed most tool errors on `hive_feedback` and some on `hive_write` with no way to tell why.

## Context

`hivemind_mcp_tool_calls_total{tool,outcome}` counts any error reply as `outcome="error"`. Most error replies are refusals the caller can act on: an agent rating its own memory (`permission_denied`, ADR 0065), an unknown id, a bad verdict, a scope above the key's trust level, a retry during a merge or rename (`fleet_busy`). A few are server-side failures: the database timed out (`store_unavailable`), the embedder was down (`embedding_unavailable`), or the tool raised. The dashboard's "Tool error share" mixed the two, so an operator could not see whether anything was broken.

## Decision

1. The counter gains a `code` label: `none` for a success, `raised` for a raise, otherwise the reply's error code. A code that is not one of the codes in `mcp/errors.py` is counted as `other`, so the label set stays bounded (about fifteen values, ten tools).
2. `outcome` stays as it was (`ok` or `error`), so existing queries keep working.
3. The shipped dashboard splits the panel: "Tool failure share" counts only `store_unavailable`, `embedding_unavailable` and `raised`; "Tool refusals per minute, by code" shows the rest by tool and code. `FAILURE_CODES` in `mcp/metrics.py` names the failure codes, and a test keeps the dashboard in step.

## Consequences

- A refusal no longer reads as an outage on the dashboard; a steady rise in one refusal code points at agent text worth fixing.
- Against an image older than this change, "Tool failure share" reads 0 and the refusal panel is empty (no `code` label); the old `outcome="error"` query still works there.
- No label names an agent, fleet, owner or key; the ADR 0104 trade-off is unchanged.
