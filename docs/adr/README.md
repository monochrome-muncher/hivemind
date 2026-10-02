# Architecture decision records

Each ADR records one decision and why it was made. ADRs are history: a
later decision never edits an earlier one, it supersedes or amends it,
and the earlier ADR gets a status note at the top pointing forward.
SPEC.md says what the system does today; read the ADR for the reason.

To add one: next free number, a `NNNN-short-slug.md` file, and the
matching SPEC.md change in the same PR (see AGENTS.md, Rules). `0038`
is unused.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-append-only-entries-with-explicit-supersession.md) | Append-only entries with explicit supersession | Accepted |
| [0002](0002-flat-org-wide-pool-with-scope-tag.md) | Flat org-wide pool with a scope tag | Superseded by 0011 |
| [0003](0003-kill-switch-is-client-side.md) | The kill switch is client-side | Amended by 0035 |
| [0004](0004-explicit-writes-only-no-passive-capture.md) | Explicit writes only (no passive capture in v1) | Accepted |
| [0005](0005-fixed-dimension-pgvector-with-operator-configured-embeddings.md) | Fixed-dimension pgvector with operator-configured embeddings | Amended by 0015 |
| [0006](0006-hybrid-rrf-retrieval-no-knowledge-graph.md) | Hybrid retrieval: RRF fusion of BM25 + dense, no knowledge graph in v1 | Amended by 0022, 0025, 0047, 0048, 0049 |
| [0007](0007-single-postgres-node-docker-compose-self-hosted.md) | One Postgres node, docker-compose, self-hosted per org | Superseded by 0026 |
| [0008](0008-per-user-keys-plus-agent-scoped-sub-keys.md) | Per-user keys plus agent-scoped sub-keys, no OAuth in v1 | Superseded by 0012 |
| [0009](0009-pg-backed-mcp-runner-with-per-agent-credentials.md) | Postgres-backed MCP runner with per-agent credentials | Amended by 0012, 0042 |
| [0010](0010-hostable-multi-agent-streamable-http-mcp-runner-with-per-request-credentials.md) | Hostable, multi-agent streamable-HTTP MCP runner with per-request credentials | Amended by 0026, 0042 |
| [0011](0011-fleets-and-trust-levels.md) | Fleets and trust levels: home-fleet scoping with a four-level privilege ladder | Accepted |
| [0012](0012-shared-org-key-with-admin-issued-agent-keys.md) | Shared org key with admin-issued agent keys | Partly superseded by 0031, 0039 |
| [0013](0013-forward-migration-path-with-schema-migrations-tracking.md) | Forward-migration path: idempotent re-apply + schema_migrations tracking | Superseded by 0020 |
| [0014](0014-bounded-retries-on-the-embedder-call.md) | Bounded retries on the embedder call | Amended by 0041 |
| [0015](0015-default-embedding-dimension-1024-with-loud-dim-mismatch-guard.md) | Default embedding dimension 1024, with a loud dim-mismatch guard | Accepted |
| [0016](0016-write-time-entity-extraction-facets-best-effort-optional-extractor.md) | Write-time entity extraction: best-effort, optional, schema-validated | Amended by 0021, 0041 |
| [0017](0017-environment-profile-dotenv-files-selected-by-environment.md) | Environment-profile dotenv files, selected by ENVIRONMENT | Accepted |
| [0018](0018-entrypoint-owned-idempotent-migration.md) | Entrypoint-owned idempotent migration (replaces the k8s initContainer) | Mechanism now 0020 |
| [0019](0019-unauthenticated-orchestrator-probe-endpoints.md) | Unauthenticated orchestrator probe endpoints | Partly superseded by 0037 |
| [0020](0020-versioned-migrations-with-rollback-and-an-advisory-lock.md) | Versioned migrations with rollback, guarded by a Postgres advisory lock | Amended by 0025; dim guard note |
| [0021](0021-embedded-text-budget-in-whitespace-words-not-model-tokens.md) | The embedded-text budget is counted in whitespace words, not model tokens | Amended by 0040 |
| [0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) | A floor under the recency factor, so match quality is the sort key | Amended by 0023 |
| [0023](0023-recency-floor-gets-a-spelling-for-no-floor.md) | `HIVEMIND_RECENCY_FLOOR` gets an explicit spelling for "no floor" | Accepted |
| [0024](0024-reject-unknown-hivemind-env-vars.md) | Reject unknown `HIVEMIND_*` environment variables | Partly superseded by 0032 |
| [0025](0025-hnsw-vector-index-approximate-nearest-neighbours.md) | An HNSW index on `entries.embedding`: approximate, not exact, nearest neighbours | Accepted |
| [0026](0026-two-app-tier-replicas-for-availability.md) | Two app-tier replicas per runner, for availability — not throughput | Accepted |
| [0027](0027-audit-log-of-admin-actions.md) | An append-only audit log of admin actions, with two actor kinds | Amended by 0028, 0046 |
| [0028](0028-revoked-agent-status-and-an-activation-guard.md) | A `revoked` agent status, and activation only from `pending` or `revoked` | Refined by 0039 |
| [0029](0029-admin-panel-runner-with-a-browser-held-key.md) | The admin panel: a static UI and an allowlisted proxy, with the key held in the browser | Accepted |
| [0030](0030-hive-whoami-lets-an-agent-see-its-own-standing.md) | `hive_whoami`: an agent can read its own standing | Accepted |
| [0031](0031-one-key-per-request-and-what-org-key-rotation-stops.md) | One key per request; rotating the org key closes registration, nothing more | Refined by 0039 |
| [0032](0032-unknown-hivemind-env-vars-warn-by-default.md) | Unknown `HIVEMIND_*` variables warn by default; failing is opt-in | Accepted |
| [0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) | Reads by id, write provenance and supersession are enforced by the server | Amended by 0040, 0060 |
| [0034](0034-supersession-targets-must-be-active.md) | A supersession target must be active | Accepted |
| [0035](0035-incognito-sessions.md) | Incognito sessions: one signal, the harness's own off switch, and marked local notes | Accepted |
| [0036](0036-foreign-entries-use-dont-relay.md) | Foreign entries: use, don't relay — an agent rule, not a server rule | Accepted |
| [0037](0037-readiness-is-shallow-the-database-probe-is-separate.md) | Readiness is shallow; the database probe is a separate endpoint | Accepted |
| [0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md) | Key lifecycle is atomic, keys are unique by schema, and a registration is owned by its alias | Accepted |
| [0040](0040-input-bounds-and-agent-name-rules.md) | Input bounds, NUL rejection and agent-name rules | Accepted |
| [0041](0041-provider-call-deadline-jitter-and-typed-embedder-errors.md) | Provider calls: an overall deadline, jittered backoff, and typed embedder errors | Accepted |
| [0042](0042-mcp-credential-is-reverified-per-call-and-fails-closed.md) | The MCP credential is re-verified on every call and a missing one fails closed | Accepted |
| [0043](0043-entries-are-untrusted-data.md) | Entries are untrusted data: framing in the instructions and the skill, not in the payload | Accepted |
| [0044](0044-key-bootstrap-writes-the-secret-directly.md) | The first-run key bootstrap writes the Kubernetes Secret itself | Accepted |
| [0045](0045-agent-names-are-unique-ignoring-case.md) | Agent names are unique ignoring case | Accepted |
| [0046](0046-registrations-are-audited.md) | Registrations are audited | Accepted |
| [0047](0047-keyword-stream-matches-any-term.md) | The keyword stream matches any query term | Amended by 0049 |
| [0048](0048-search-falls-back-to-keywords-when-the-embedder-is-down.md) | Search falls back to keywords when the embedder is down | Accepted |
| [0049](0049-keyword-stream-matches-at-most-16-terms.md) | The keyword stream matches at most 16 distinct query terms | Accepted |
| [0050](0050-prometheus-scrape-endpoint.md) | The REST runner serves a Prometheus scrape endpoint | Accepted |
| [0051](0051-feedback-is-readable.md) | Feedback is readable by the entry's audience | Accepted |
| [0052](0052-writes-report-the-nearest-entries.md) | Writes report the nearest existing entries | Accepted |
| [0053](0053-feedback-on-several-entries-at-once.md) | Feedback on several entries at once | Accepted |
| [0054](0054-a-flagged-filter-for-entries-reported-stale-or-wrong.md) | A `flagged` filter for entries reported stale or wrong | Accepted |
| [0055](0055-read-several-entries-in-one-call.md) | Read several entries in one call | Amended by 0059 |
| [0056](0056-count-searches-that-find-nothing.md) | Count searches that find nothing | Accepted |
| [0057](0057-see-also-links-between-entries.md) | "See also" links between entries | Accepted |
| [0058](0058-pinned-fleet-briefing.md) | A pinned briefing per fleet | Accepted |
| [0059](0059-batch-reads-use-a-fixed-number-of-store-reads.md) | Batch reads use a fixed number of store reads | Accepted |
| [0060](0060-the-admin-key-writes-self-or-org-entries.md) | The admin key writes `self` or `org` entries | Accepted |
| [0061](0061-vector-search-needs-positive-similarity.md) | Vector search needs a positive similarity | Amended by 0062 |
| [0062](0062-a-similarity-threshold-for-the-vector-stream.md) | A similarity threshold for the vector stream | Accepted |
