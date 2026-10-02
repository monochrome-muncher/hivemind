# Read several entries in one call

> **Status: §5 is amended by [ADR 0059](0059-batch-reads-use-a-fixed-number-of-store-reads.md):**
> the feedback and links of a batch are read in a fixed number of store
> reads, not one per entry.

## Context

Retrieval is two-stage (SPEC §6.1): search returns compact hits, and the
agent opens the ones it wants with `hive_get`. `hive_get` took one id, so
opening five hits cost five tool calls, each a model round trip. That
cost pushes agents to open fewer hits than they should, and the
progressive-disclosure design only works if opening is cheap.

## Decision

1. **`hive_get` takes `entry_ids` (up to 10) instead of `entry_id`** and
   answers `{entries, not_found}`. REST gains `POST /v1/entries/get` with
   `{entry_ids}` and the same answer. Each entry is the same as a single
   read returns, `feedback` included (ADR 0051). The single-entry forms
   are unchanged.

2. **`entries` keeps the order asked**, with repeated ids once.
   **`not_found` lists the ids that name nothing the caller may read**:
   unknown and invisible are the same answer, as for a single read
   (ADR 0033). A read is not all-or-nothing, unlike batch feedback
   (ADR 0053): a missing entry changes nothing, so the others are still
   worth returning.

3. **10 ids at most** (`MAX_GET_IDS`). A body can be 100 000 characters
   (ADR 0040), so this bounds one reply to about a million characters;
   10 covers a page of hits at the default search limit.

4. **`include_history` is not available on a batch read.** A chain walk
   per id would multiply the cost the ADR 0040 bounds were set against;
   open one entry to see its history.

5. **One store read for the entries** (`Store.get_entries`, which the
   supersession check already uses), keyed back to the ids as the caller
   spelled them, then one feedback read per entry.

## Consequences

- Opening a page of hits is one call.
- A reply can be large: the agent chooses how many hits to open.
- REST's batch read is a `POST`, because the ids belong in a body, not a
  query string.
