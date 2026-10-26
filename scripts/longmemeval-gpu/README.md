# LongMemEval on a GPU

`make measure-longmemeval` ([tests/eval/longmemeval.py](../../tests/eval/longmemeval.py))
embeds one text at a time over HTTP. LongMemEval_S has about 18,000 distinct
sessions, around 50 million tokens, so on a CPU that takes days. These two files
run the same measurement with the embeddings computed in batches on a GPU.

- [lme_gpu.py](lme_gpu.py) uses the harness's own code to list every text the
  harness will embed, along with its cache key. It then embeds them in batches
  with `transformers` (fp16), writes them into the harness's disk cache
  ([tests/eval/embed_cache.py](../../tests/eval/embed_cache.py)), and runs the
  harness with every vector already cached. A run that stops can be started
  again and picks up where it left off. Qwen3-Embedding-8B overflows in fp16 on a few
  texts whatever their length; those are embedded again in fp32 (weights spread
  over the GPUs and CPU memory) and counted in `longmemeval-<tag>-notes.txt`.
- [hivemind-longmemeval.ipynb](hivemind-longmemeval.ipynb) is a Kaggle notebook
  that clones the repository, downloads the dataset and runs `lme_gpu.py`. It
  runs Qwen3-Embedding-0.6B (dim 1024) on all 470 answerable questions, then
  Qwen3-Embedding-8B (dim 2000, the largest the index allows) on 20 questions
  per type. Kaggle's free GPU quota covers both. The steps are in the
  notebook's first cell.
- [hivemind-longmemeval-8b-all.ipynb](hivemind-longmemeval-8b-all.ipynb) runs
  Qwen3-Embedding-8B on all 470 questions. That is about 35 million tokens,
  9 to 10 hours on two T4s, close to Kaggle's 12-hour session limit, so the run
  stops itself after a time budget (`time_budget_hours`) and keeps its vector
  cache in the version's output. Adding that output as an input to the next run
  carries on where it stopped (`LME_SEED` points at the inputs, and every cache
  found there is merged in).
  The same route reruns the harness after it changes (a new variant, say)
  without embedding everything again; `max_new_texts` stops a run early when
  the earlier cache isn't attached. The steps are in the notebook's first cell.

Elsewhere, run `python lme_gpu.py '<runs as JSON>'` on any machine that has a
GPU, `torch`, `transformers` and `uv`, setting `LME_REPO`, `LME_DATA`,
`LME_WORK` and `LME_OUT` to the checkout, the dataset file, a scratch directory
and the output directory. `LME_CACHE` moves the vector caches (default
`LME_WORK`) and `LME_SEED` names a directory searched for earlier
`cache-<tag>/embeddings.sqlite3` files to merge in. The runs JSON format is in the notebook's last cell.

The vectors match the llama.cpp Q8_0 GGUF served over HTTP (cosine ≥ 0.998 on
spot checks), so results from either route are comparable.
