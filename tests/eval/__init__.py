"""The retrieval eval harness (ROADMAP §1.1).

A hermetic eval: the golden corpus (``tests/eval/golden.py``) is seeded
into a ``MemoryStore`` (deterministic ids, no Postgres, no network), run
through ``SearchService``, and the retrieval metrics (hit@k / MRR / nDCG,
``hivemind.retrieval.eval``) are computed over the golden query set. The
CI gate pins the metrics so a future retrieval change is *measured*,
not vibes.
"""
