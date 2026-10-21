# The vector similarity cut-off is on by default

Amends [ADR 0062](0062-a-similarity-threshold-for-the-vector-stream.md) §1 (the setting is no longer off by default) and builds on [ADR 0097](0097-query-instruction-for-instruction-aware-embedders.md). Changes one default; adds no setting.

Status: Accepted. `HIVEMIND_VECTOR_MIN_SIMILARITY` defaults to 0.45; an empty value turns it off.

## Context

ADR 0062 added a cut-off for the vector stream and left it off, because the right value depends on the embedding model and the measurements did not yet agree on one. Without it, a real embedding model gives almost every entry a positive similarity to almost every query, so nearly every search returns hits and an agent cannot tell "the hive has an answer" from "the hive has nothing on this".

The query instruction (ADR 0097) changed the measurement. It lowers the similarity of the right answer a little and the similarity of an off-topic query's nearest entry much more, so a cut-off separates the two better. Measured on Qwen3-Embedding 8B with the instruction on (`docs/retrieval-experiments.md`, "Query instruction (ADR 0097)"):

| cut-off | answerable searches that keep their answer on the vector stream | off-topic searches with an empty vector stream, single-domain pools |
|---|---|---|
| none | 99% | 4% |
| 0.45 | 98% | 55% |
| 0.50 | 97% | |
| 0.55 | 92% | |

0.45 gives up one point of answers and empties more than half of the off-topic vector streams. 0.50 and 0.55 lose more answers for little extra.

## Decision

1. **Default 0.45.** `SearchConfig.vector_min_similarity` and `Settings.vector_min_similarity` default to `DEFAULT_VECTOR_MIN_SIMILARITY = 0.45`. This is the value measured for the Qwen3-Embedding family at 1024 dimensions.
2. **Vector stream only.** As in ADR 0062, `Store.search_vector` leaves out entries at or below the cut-off. The keyword stream is not cut, so an entry sharing a word with the query still comes back, and a search is empty only when neither stream finds anything (counted by ADR 0056). `similar_entries` (related entries on write, ADR 0052) is not affected.
3. **Turning it off.** Unset means 0.45. An explicitly empty value, or `none` in any case, means off: the vector stream keeps every entry with a positive similarity (ADR 0061). The values `0` and `0.0` are accepted and have the same effect. Values outside `[0, 1)` still fail at startup (ADR 0024).
4. **Another embedder re-measures.** 0.45 is a Qwen3 value. With a different model or dimension, run `make measure-threshold` ([retrieval experiments](../retrieval-experiments.md#similarity-threshold)) and set the suggested value, or turn the cut-off off. Changing the query instruction moves query vectors, so re-check it then too (ADR 0097).
5. **Test configs.** `make_search_config()` in `tests/fakes.py` sets the cut-off to `None`, as it does for the query instruction, because the hash embedder's similarities are not Qwen-like and 0.45 would empty its vector stream. The measurement harnesses that compare something else (importance, query instruction, confirmation) hold the cut-off off for the same reason; the threshold harness sets it for each step of its sweep.

## Consequences

- On upgrade, with no configuration change, a deployment on 0.45 sees ranking and result counts change: weak vector matches disappear, and a search with nothing relevant can come back empty or with keyword matches only. The empty-search counter (ADR 0056) will rise.
- About 2% of answerable searches lose their answer from the vector stream on 8B; the keyword stream still carries them if they share a word with the query. Vague questions are hit first.
- A deployment on a non-Qwen embedder that does not set the value gets a cut-off that was never measured for it. It can drop real answers (a model with lower similarities) or cut nothing (a model with higher ones). Operators with such a model should set the value or turn it off before upgrading.
- The numbers come from synthetic corpora (`tests/eval/corpus`). Check the value against the empty-search counter and agents' feedback, as ADR 0062 said.
