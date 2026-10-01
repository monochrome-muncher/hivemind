# Writes report the nearest existing entries

## Context

"Supersede, don't duplicate" (SPEC §5.2) depends entirely on the writer
searching first. An agent that skips the search, or searches with other
words, writes a near-duplicate beside the original. Both then rank in
search, possibly saying different things, with nothing linking them.

The write path already computes the new entry's embedding (SPEC §7). One
more nearest-neighbour query over that vector finds the entries closest to
the new one at almost no cost.

SPEC §10 holds **LLM-assisted contradiction detection** until a trigger
fires (pool > 10k entries or an observed incident), because an untuned
similarity floor would misfire. This decision is deliberately smaller.

## Decision

1. **Every successful write returns `related`**: up to 3 active entries
   nearest to the new one, each with `id, kind, summary, author, scope,
   fleet_id, occurred_at` and its cosine `similarity` (rounded to 3
   places). REST `POST /v1/entries` and MCP `hive_write` both carry it.
   The new entry itself and inactive entries are left out.

2. **Only entries the writer can read** (ADR 0011 visibility): the lookup
   runs under the writer's visibility, like a search.

3. **No floor, no verdict, no action.** The list is the nearest entries,
   not a claim that they are related or contradictory. The writer judges:
   the skill and tool description say to withdraw a duplicate, and to
   withdraw and rewrite with `supersedes` when the new entry corrects or
   extends one. Nothing is refused, merged or superseded automatically
   (ADR 0001).

4. **Best-effort.** The entry is stored before the lookup runs; a failed
   lookup is logged and the write answers with an empty `related`.

5. It uses the same HNSW index and per-query settings as the vector stream,
   so it is approximate in the same way (ADR 0025).

## Why this does not fire SPEC §10's contradiction-detection trigger

That extension judges conflict, needs an LLM, and needs a tuned floor to
decide when to speak. This one does none of those: it always reports the
three nearest entries with their similarity and leaves the judgement to
the writer, so there is no floor to mistune.

## Alternatives considered

- **Refuse near-duplicates at write time** (Caura refuses at cosine ≥ 0.97
  and sends 0.85–0.97 to an LLM judge). Rejected for now: it needs a floor
  tuned on a real corpus, and a refusal on an untuned floor blocks
  legitimate writes.
- **A pre-write check verb.** Rejected: a ninth verb, and a second
  embedding call, for what the write's reply already carries. The cost is
  that fixing a duplicate takes a withdraw and a rewrite.

## Consequences

- One extra indexed query per write, plus one batched read of at most 3
  entries.
- A write's reply is larger by up to 3 compact entries.
- Duplicates become visible to the agent that just wrote one, at the moment
  it can still fix it.
