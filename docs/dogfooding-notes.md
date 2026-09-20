# Dogfooding notes — end-to-end run with a real agent + real embedder

*ROADMAP §1.2. Run 2026-09-20 by `pi` (the dogfooding agent itself) against the
local stack: Postgres (pgvector, **512-dim**) + vLLM `Qwen3-Embedding-0.6B`
(`:8001`) + the hostable `hivemind-mcp-http` runner (`:8088`, per-request
bearer, ADR 0010). The agent was provisioned as an L2 contributor (`pi-1`,
home fleet `analysts`) exactly the way a real operator would be: admin key →
fleet → register → activate → agent key.*

## The loop that was run

```
list tools
→ hive_write (fact, no scope)          # the "unthinked" write
→ hive_write (insight, scope=fleet)
→ hive_write (decision, scope=fleet)
→ hive_search × 3 (semantic, zero keyword overlap with the stored summaries)
→ hive_get (the decision entry)
→ hive_feedback (helpful, on the insight)
→ hive_write (supersession: corrected decision)
→ hive_search (re-check: supersession invariant — v2 ranks #1)
→ probes: empty entry_id, L2 + explicit scope=org
```

All via the official `mcp` SDK streamable-HTTP client (what a real MCP client
does), with the agent's L2 key in the `Authorization` header.

## What worked (the core is sound)

- **Provisioning via REST is clean.** admin key → fleet → register → activate
  (L2 + home fleet) → agent key, all in one pass; the key works on the very
  first MCP request. ADR 0010's per-request auth is transparent to the client.
- **Semantic retrieval is real.** "what size are the model vectors" (zero
  keyword overlap with "embedding dim … 512") ranked the decision entry first
  via the vector stream; "how do we know search quality hasn't regressed" →
  the eval-gate insight. The keyword + vector fusion (ADR 0003, RRF) works on
  a real embedder, not just the deterministic fake.
- **Supersession invariant holds.** after the corrected decision superseded
  the original, the re-search ranked the successor first.
- **Latency is fine at this scale.** write ~60–100 ms (vLLM embedding
  included), search ~35–45 ms, get/feedback ~3–8 ms. The inline-embedding
  write path (ADR 0005) is comfortable for interactive agents.
- **Error messages are actionable.** "trust level 2 may not write scope
  'org' (ADR 0011)" tells an LLM exactly what to fix; the invalid-input
  guards name the offending field.

## Friction found (and what was done about it)

| # | Friction | Severity | Disposition |
|---|----------|----------|-------------|
| 1 | **`hive_write`'s default `scope="org"` broke ADR 0011's omitted-scope rule.** An L2 agent that writes *without thinking about scope* (the common case) got `permission_denied: trust level 2 may not write scope 'org'` — the tool signature (MCP wrapper) and the REST schema both injected a forced `"org"` default, bypassing the "omitted → highest permitted" resolution. The bug lived in the *registered wrapper* (`mcp/server.py`), not the app-layer function — so the app-layer unit tests never saw it; only an end-to-end run did. | High | **Fixed:** `scope: str \| None = None` in the app function, the MCP wrapper, and the REST schema; omitted scope now resolves to the highest scope the trust level permits (L1→self, L2/L3→fleet, legacy/admin→org). Covered by tests at all three seams (app, registered-tool, REST). |
| 2 | **Empty `entry_id` produced `unknown entry: `** (a not_found with an empty name) instead of a caller-error. | Medium | **Fixed:** `hive_get`/`hive_feedback` now return `invalid_input: entry_id is required` on an empty id. |
| 3 | **Entry reads didn't expose `fleet_id`** even though entries are fleet-scoped — an agent couldn't tell which fleet an entry belonged to from `hive_get`/REST responses. | Medium | **Fixed:** `fleet_id` added to the MCP entry dict and the REST `EntryOut`. |
| 4 | **The 280-char summary cap is tight for corrections.** A realistic "correcting decision" summary (306 chars, then 283) was rejected — the error message was clear, but the cap is easily hit by an agent writing a normal-length summary. | Low | Noted; the tool description now documents the cap. Raising the cap is a deliberate choice (summary = embedded text = relevance signal) — not changed. |
| 5 | **Docker build caching masked a code fix.** `docker compose build` + `up -d` reused a cached layer, so the container kept serving stale code until a `--no-cache` rebuild. | Ops | Noted: use `docker compose build --no-cache mcp-http` (or verify the running code) after source changes. |
| 6 | **The pi MCP gateway caches the bearer token.** After a key rotation / re-provision, the in-session `mcp` tool still sent the old token (401) while a direct curl with the new key worked (200). | Ops | Noted: restart the gateway (or re-install the MCP server) after rotating the key; the gateway's own `connect` re-auth path is a separate (pi-side) concern. |
| 7 | **REST and MCP use different auth headers.** REST = `X-API-Key`; MCP streamable-HTTP = `Authorization: Bearer`. An agent driving both surfaces must know both. | Low | Noted: both are documented per-surface; unifying is a wire-protocol decision (not made). |
| 8 | **The integration suite wipes the dev pool.** Running `make test` (integration) re-migrates the shared local Postgres, so dogfood identities/entries disappear and the agent key must be re-provisioned. Also: bare `uv run pytest` (no Makefile env) assumes the 1536-dim default, while the Makefile exports `HIVEMIND_EMBEDDING_DIM=512` — a dim-mismatch footgun for anyone running the suite outside `make`. | Ops | Noted as a follow-up: a clear "dim mismatch" error (or auto-reset) for the integration suite; document `HIVEMIND_EMBEDDING_DIM` on the `make test` path. |

## What the run did *not* cover

- Multi-agent concurrent load (this was a single sequential agent).
- The 1024-dim production embedder (the run used 512; 1024 is a deploy-time
  choice per ADR 0005 — same loop, different pool).
- Key rotation mid-session (the key was stable for the whole run).

## Bottom line

The core write → search → feedback → supersede loop is **sound on a real
embedder**, and the access model (ADRs 0011–0012) is exactly as specified —
the one real defect (the forced `"org"` scope default) was a wiring bug that
only a real end-to-end run could surface, and it is now fixed and covered at
every seam. The remaining items are ops polish (build cache, token cache,
pool reset, dim footgun), not product defects.