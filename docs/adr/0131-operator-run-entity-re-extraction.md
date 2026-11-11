# An operator-run command re-extracts the entity facets of stored entries

Amends [ADR 0016](0016-write-time-entity-extraction-facets-best-effort-optional-extractor.md) (no backfill) and [ADR 0130](0130-extractor-prompt-defines-kinds-and-name-rules-with-worked-examples.md) (old and new spellings coexist). Follows the [ADR 0005](0005-fixed-dimension-pgvector-with-operator-configured-embeddings.md) re-embedding pattern, the one in-place exception to [ADR 0001](0001-append-only-entries-with-explicit-supersession.md) for machine metadata.

Status: Accepted.

## Context

ADR 0130 changed how the extractor writes entity names ("PostgreSQL", not "Postgres"). Entries extracted before it keep their old names, and entries written while extraction was off or failing have none. The entity filter matches names exactly, so a filter on "PostgreSQL" misses the older entries and a filter on "Postgres" misses the newer ones. ADR 0016 left a backfill as a named maintenance procedure, to be built when the pool needed it. It needs it now.

## Decision

1. A console command, `hivemind-reextract`, in the same image as every runner (`HIVEMIND_RUNNER=reextract`). An operator runs it once, as a Kubernetes Job or from a shell with the deployment's settings. It is not an HTTP route: a pool-wide run takes as long as one extractor call per entry, far past any request timeout, and the admin panel stays a view over the REST API.
2. It is a dry run unless `--apply` is given. The dry run calls the extractor and prints, per entry, the names it has and the names it would get.
3. It reads entries in `(created_at, id)` order, in batches (`--batch-size`, default 50) with a few extractor calls in flight (`--concurrency`, default 4). After each batch it prints a resume point; `--after <entry id>` starts after it. `--limit` stops after a number of entries.
4. Scope: active entries by default, `--include-inactive` adds superseded and withdrawn ones. `--fleet` and `--created-after` / `--created-before` narrow it. Redacted entries are never read or written (ADR 0125).
5. It writes only `entities`, `entity_names` and `entities_model`, and only for an entry whose set of names (ignoring letter case) and kinds differs from the stored one; a new order alone is not a change. The rest of the entry, its embedding and its keyword index are untouched. Each batch's writes are one short transaction with a 5 s lock timeout, and the update skips a row redacted since it was read. Running it twice changes nothing the second time.
6. A result that would empty an entry's facets is reported and kept back unless `--allow-clear` is given. A model that answers `[]` to everything is valid output, not a failure, and must not wipe the pool.
7. An extraction that fails keeps the entry's facets and is reported. A batch where every extraction fails, or whose write hits a lock timeout or deadlock, stops the run before writing, with the last finished batch as the resume point, so a dead extractor does not walk the whole pool.
8. It connects without prepared-statement caching, like the service pool, so it works behind a transaction-mode PgBouncer.
9. No audit row. Like re-embedding, this is machine metadata with its provenance in `entities_model`; the Job log is the record. No migration.

## Considered and not done

- **An admin REST route or panel button.** It would need a background job runner with state in the database, a new kind of thing for Hivemind, for an action run once per prompt or model change.
- **A marker column for "extracted with prompt N".** It would let a rerun skip finished entries without `--after`, but costs a migration for a one-off. A `--created-before` the date the new prompt shipped selects the same entries, and a full rerun is safe, only slower.
- **Re-extracting on read or in the background.** That is LLM enrichment outside the write path, which this project rejects.

## Consequences

- An operator can bring the pool to one spelling per entity after a prompt or model change, at the cost of one extractor call per entry in scope (twice, counting the dry run; `--limit` keeps a first dry run small).
- `entities_model` names the model that produced the stored list. An entry this command leaves unchanged keeps its old value. The time of a re-extraction is in the Job log only.
- Entries stay immutable to agents. Only the machine-derived facets change, and only by an operator's hand.
