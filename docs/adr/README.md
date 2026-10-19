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
| [0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) | A floor under the recency factor, so match quality is the sort key | Amended by 0023, 0070, 0071, 0072, 0073, 0088 |
| [0023](0023-recency-floor-gets-a-spelling-for-no-floor.md) | `HIVEMIND_RECENCY_FLOOR` gets an explicit spelling for "no floor" | Accepted |
| [0024](0024-reject-unknown-hivemind-env-vars.md) | Reject unknown `HIVEMIND_*` environment variables | Partly superseded by 0032 |
| [0025](0025-hnsw-vector-index-approximate-nearest-neighbours.md) | An HNSW index on `entries.embedding`: approximate, not exact, nearest neighbours | Accepted |
| [0026](0026-two-app-tier-replicas-for-availability.md) | Two app-tier replicas per runner, for availability — not throughput | Accepted |
| [0027](0027-audit-log-of-admin-actions.md) | An append-only audit log of admin actions, with two actor kinds | Amended by 0028, 0046, 0076, 0095 |
| [0028](0028-revoked-agent-status-and-an-activation-guard.md) | A `revoked` agent status, and activation only from `pending` or `revoked` | Refined by 0039 |
| [0029](0029-admin-panel-runner-with-a-browser-held-key.md) | The admin panel: a static UI and an allowlisted proxy, with the key held in the browser | Amended by 0080, 0091; the agent portal (0095) is built the same way |
| [0030](0030-hive-whoami-lets-an-agent-see-its-own-standing.md) | `hive_whoami`: an agent can read its own standing | Accepted |
| [0031](0031-one-key-per-request-and-what-org-key-rotation-stops.md) | One key per request; rotating the org key closes registration, nothing more | Refined by 0039 |
| [0032](0032-unknown-hivemind-env-vars-warn-by-default.md) | Unknown `HIVEMIND_*` variables warn by default; failing is opt-in | Accepted |
| [0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) | Reads by id, write provenance and supersession are enforced by the server | Amended by 0040, 0060 |
| [0034](0034-supersession-targets-must-be-active.md) | A supersession target must be active | Accepted |
| [0035](0035-incognito-sessions.md) | Incognito sessions: one signal, the harness's own off switch, and marked local notes | Accepted |
| [0036](0036-foreign-entries-use-dont-relay.md) | Foreign entries: use, don't relay — an agent rule, not a server rule | Accepted |
| [0037](0037-readiness-is-shallow-the-database-probe-is-separate.md) | Readiness is shallow; the database probe is a separate endpoint | Accepted |
| [0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md) | Key lifecycle is atomic, keys are unique by schema, and a registration is owned by its alias | Accepted |
| [0040](0040-input-bounds-and-agent-name-rules.md) | Input bounds, NUL rejection and agent-name rules | Amended by 0045 |
| [0041](0041-provider-call-deadline-jitter-and-typed-embedder-errors.md) | Provider calls: an overall deadline, jittered backoff, and typed embedder errors | Amended by 0048 |
| [0042](0042-mcp-credential-is-reverified-per-call-and-fails-closed.md) | The MCP credential is re-verified on every call and a missing one fails closed | Accepted |
| [0043](0043-entries-are-untrusted-data.md) | Entries are untrusted data: framing in the instructions and the skill, not in the payload | Accepted |
| [0044](0044-key-bootstrap-writes-the-secret-directly.md) | The first-run key bootstrap writes the Kubernetes Secret itself | Accepted |
| [0045](0045-agent-names-are-unique-ignoring-case.md) | Agent names are unique ignoring case | Accepted |
| [0046](0046-registrations-are-audited.md) | Registrations are audited | Accepted |
| [0047](0047-keyword-stream-matches-any-term.md) | The keyword stream matches any query term | Amended by 0049 |
| [0048](0048-search-falls-back-to-keywords-when-the-embedder-is-down.md) | Search falls back to keywords when the embedder is down | Accepted |
| [0049](0049-keyword-stream-matches-at-most-16-terms.md) | The keyword stream matches at most 16 distinct query terms | Accepted |
| [0050](0050-prometheus-scrape-endpoint.md) | The REST runner serves a Prometheus scrape endpoint | Amended by 0069 |
| [0051](0051-feedback-is-readable.md) | Feedback is readable by the entry's audience | Amended by 0064 |
| [0052](0052-writes-report-the-nearest-entries.md) | Writes report the nearest existing entries | Amended by 0077, 0087 |
| [0053](0053-feedback-on-several-entries-at-once.md) | Feedback on several entries at once | Amended by 0065 |
| [0054](0054-a-flagged-filter-for-entries-reported-stale-or-wrong.md) | A `flagged` filter for entries reported stale or wrong | Accepted |
| [0055](0055-read-several-entries-in-one-call.md) | Read several entries in one call | Amended by 0059 |
| [0056](0056-count-searches-that-find-nothing.md) | Count searches that find nothing | Accepted |
| [0057](0057-see-also-links-between-entries.md) | "See also" links between entries | Accepted |
| [0058](0058-pinned-fleet-briefing.md) | A pinned briefing per fleet | Amended by 0076 |
| [0059](0059-batch-reads-use-a-fixed-number-of-store-reads.md) | Batch reads use a fixed number of store reads | Accepted |
| [0060](0060-the-admin-key-writes-self-or-org-entries.md) | The admin key writes `self` or `org` entries | Amended by 0066 |
| [0061](0061-vector-search-needs-positive-similarity.md) | Vector search needs a positive similarity | Amended by 0062 |
| [0062](0062-a-similarity-threshold-for-the-vector-stream.md) | A similarity threshold for the vector stream | Accepted |
| [0063](0063-feedback-quality-by-consensus-share.md) | Feedback quality by consensus share | Amended by 0070 |
| [0064](0064-corrections-stay-visible-in-recent-feedback.md) | Corrections stay visible in an entry's recent feedback | Accepted |
| [0065](0065-an-author-cannot-rate-its-own-entry.md) | An author cannot rate its own entry | Accepted |
| [0066](0066-the-admin-key-has-one-feedback-vote.md) | The admin key has one feedback vote per entry | Accepted |
| [0067](0067-a-revoked-agents-feedback-no-longer-counts.md) | A revoked agent's feedback no longer counts | Accepted |
| [0068](0068-rater-flag-confirmation-metric.md) | Count the flags a later supersession confirmed | Extended by 0075 |
| [0069](0069-cache-the-entry-scan-behind-the-usage-report.md) | Cache the entry scan behind the usage report | Extended by 0075 |
| [0070](0070-one-capped-budget-for-social-signals.md) | One capped budget for social signals | Amended by 0071, 0072, 0073, 0088 |
| [0071](0071-a-confirmation-can-restart-the-recency-clock.md) | A confirmation can restart the recency clock | Accepted (off by default) |
| [0072](0072-recency-profile-per-kind.md) | A recency profile per kind | Accepted (off by default); amended by 0073 |
| [0073](0073-a-recency-hint-on-search.md) | A recency hint on search | Accepted (off by default) |
| [0074](0074-group-unlinked-versions-at-read-time.md) | Group unlinked versions at read time | Accepted (off by default) |
| [0075](0075-fleet-health-metrics.md) | Fleet health metrics: admin-only and observation-only | Extended by 0089 |
| [0076](0076-pin-integrity.md) | Pin integrity: audited pins and "changed since pinned" | Amended by 0079 |
| [0077](0077-write-check-mode-and-warnings.md) | Writes can be checked first, and answer with advisory warnings | Amended by 0087 |
| [0078](0078-explore-search.md) | Opt-in explore search | Accepted |
| [0079](0079-feedback-dates-on-the-database-clock.md) | Feedback is dated by the store's clock | Accepted |
| [0080](0080-admin-memory-browser-and-activity.md) | The admin panel can read memories and show activity | Extended by 0090, 0092 |
| [0081](0081-upstream-changed-on-hits.md) | A hit shows when an entry it links to changed | Accepted |
| [0082](0082-dev-mode-credentials-sit-at-trust-level-zero.md) | Dev-mode credentials sit at trust level 0 when the pool requires stored keys | Accepted (off by default) |
| [0083](0083-the-entry-read-path-is-serviced.md) | The entry-read path is serviced | Accepted |
| [0084](0084-the-wire-contract-is-single-sourced-and-the-usage-report-is-typed.md) | The wire contract is single-sourced, and the usage report is typed | Accepted |
| [0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) | The MCP package splits into contract, server assembly, and runners | Accepted |
| [0086](0086-key-hashes-stay-sha-256.md) | Key hashes stay SHA-256 (the KDF upgrade is held) | Accepted |
| [0087](0087-near-duplicate-write-warning.md) | A write warns when its nearest entry is a near-duplicate | Accepted |
| [0088](0088-narrow-importance.md) | A narrower importance factor, so one importance step stays below the peer cap | Accepted |
| [0089](0089-owner-level-rating-metrics.md) | Owner-level rating metrics: sibling verdicts and owner concentration (admin-only, observation-only) | Accepted |
| [0090](0090-admin-feedback-log.md) | The admin panel shows who rated what (admin-only, observation-only) | Accepted |
| [0091](0091-admin-panel-shared-front-end-modules.md) | The admin panel's generic modules know nothing about the admin | Accepted |
| [0092](0092-admin-fleet-pins.md) | The admin panel shows each fleet's pins (admin-only, read-only) | Accepted |
| [0093](0093-admin-flag-backlog.md) | The admin panel lists the flag backlog, oldest flag first (admin-only, observation-only) | Accepted |
| [0094](0094-admin-csv-export.md) | The admin panel exports its lists as CSV (front end only) | Accepted |
| [0095](0095-agent-portal.md) | The agent portal: a browser front end for agent-key holders | Accepted |
