# The entry-read path is serviced

Every data plane has a service module: writes (`WriteService`), search
(`SearchService`), feedback/pins/withdrawal (`GovernanceService`),
registration (`AccessService`) — but the entry-read path (read by id, batch
read, filtered list) has none. Both surfaces orchestrate the same
composition themselves:

* **single entry** (REST `_entry_detail` ≡ MCP `hive_get`): a
  visibility-checked read (ADR 0033), then `feedback_summary` +
  `entry_links` in parallel, then the `supersession_chain` when history is
  asked for;
* **batch** (REST `POST /v1/entries/get` ≡ MCP `hive_get` with
  `entry_ids`): dedupe + the `MAX_GET_IDS` cap (ADR 0055),
  `get_visible_entries`, then `feedback_summaries` + `entries_links` in
  parallel — a fixed number of store reads for the whole batch (ADR 0059);
* **list** (REST `GET /v1/entries` ≡ the admin entry list ≡ MCP
  `hive_list`): filter construction + validation (ADR 0040), default-limit
  handling, `store.list_entries`.

The surfaces call the `Store` directly and import `chain.py`'s building
blocks themselves, where the architecture map says surfaces are thin
(validation + auth + error mapping only). The ADR 0059 read-count
invariant is held by two surface copies; read-path behaviour is pinned by
surface tests, not service tests; and a read-composition change lands in N
places.

## Decision

1. **New `services/read.py` — `ReadService`**, with three methods:
   * `entry(entry_id, *, visibility, include_history=False)` →
     `EntryDetail | None` (`None` = invisible or unknown — the same answer,
     ADR 0033); `EntryDetail` carries the entry, its `FeedbackSummary`, its
     `EntryLinks`, and (with history) the `supersession_chain`'s
     successors/superseded;
   * `entries(entry_ids, *, visibility)` → `EntryBatch`: dedupes, enforces
     the `MAX_GET_IDS` cap (`InvalidInput`), `EntryDetail`s in the order
     asked, `not_found` for the rest (ADRs 0055, 0059);
   * `list(filters, *, limit=None, offset=0, visibility)` → the filtered
     entries: validates the filters (ADR 0040) and applies
     `search_config.default_limit` when `limit` is omitted; a surface that
     has its own default (the admin list) passes it explicitly.
2. **Composed in `build_services`** (store + `GovernanceService` for the
   feedback summaries + `search_config` for the default limit);
   `McpHivemind` and `HivemindApp` carry it. The surfaces serialize only —
   the REST `EntryOut`/`EntriesOut` and the MCP dict builders are
   unchanged, so no wire shape moves in this ADR.
3. **`chain.py` stays the shared building block** (visibility-checked
   reads, links, the chain walk) that `ReadService` composes; it is not
   moved.
4. **Error semantics unchanged**: invisible ≡ not found (404 /
   `not_found`); an empty or over-cap batch is `InvalidInput` (422 /
   `invalid_input`), as today.

## Consequences

* The ADR 0059 read-count invariant (a fixed number of store reads per
  batch, independent of batch size) is now testable at the service seam.
* Surface tests stay green: this moves orchestration, not serialisation.
* One new service module; `Services` gains `read_service`. The admin gate
  (`_require_admin`) stays in the routes — the service takes the verified
  credential's visibility, exactly as the write path does.
