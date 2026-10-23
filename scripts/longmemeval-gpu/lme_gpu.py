# LongMemEval search-only run for Hivemind, with the embeddings computed on a GPU.
#
# The harness (`make measure-longmemeval`, tests/eval/longmemeval.py) embeds one
# text at a time over HTTP, which is slow. Here every text it will ask for is
# written out first (by the harness's own code, so the texts and cache keys are
# exact), embedded in batches on the GPU, and stored in the harness's disk cache
# (tests/eval/embed_cache.py). The harness then runs with every vector cached.

import gc
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("LME_REPO", "/tmp/hivemind"))
DATA = Path(os.environ.get("LME_DATA", "/tmp/lme/longmemeval_s_cleaned.json"))
WORK = Path(os.environ.get("LME_WORK", "/tmp/lme"))
OUT = Path(os.environ.get("LME_OUT", "/kaggle/working"))
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# Runs the harness's code to list the (cache key, text) pairs one model needs.
DUMP = r'''
import json, os, sqlite3, sys
from hivemind.config import SearchConfig, load_settings
from hivemind.embeddings import build_embedder
from tests.eval.embed_cache import CACHE_FILE, DiskCachingEmbedder
from tests.eval.longmemeval import (dataset_path, is_abstention, load_questions,
    parse_date, per_type_limit, select, session_draft)

settings = load_settings()
cache = DiskCachingEmbedder(build_embedder(settings), os.environ["EVAL_EMBED_CACHE"])
questions = [q for q in select(load_questions(dataset_path()),
             per_type_limit(os.environ.get("EVAL_LONGMEMEVAL_PER_TYPE")))
             if not is_abstention(q)]
query = SearchConfig().embedded_query  # the variants all start from SearchConfig()
done = {r[0] for r in cache._db.execute("SELECT key FROM vectors")}
seen = set()
with open(sys.argv[1], "w") as out:
    for q in questions:
        items = [("t", query(q["question"]))]
        for session, date in zip(q["haystack_sessions"], q["haystack_dates"], strict=True):
            items.append(("e", cache.entry_embeddable_text(session_draft(session, parse_date(date)))))
        for kind, text in items:
            key = cache._key(kind, text)
            if key not in done and key not in seen:
                seen.add(key)
                out.write(json.dumps({"key": key, "text": text}) + "\n")
print(len(questions), "questions,", len(seen), "texts to embed,", len(done), "already cached")
'''


def harness_env(run, cache_dir):
    env = dict(os.environ)
    env.update(
        HIVEMIND_EMBEDDING_ENDPOINT="http://127.0.0.1:9/v1",  # never called: every vector is cached
        HIVEMIND_EMBEDDING_MODEL=run["model"],
        HIVEMIND_EMBEDDING_DIM=str(run["dim"]),
        HIVEMIND_EMBEDDING_API_KEY="unused",
        EVAL_LONGMEMEVAL_PATH=str(DATA),
        EVAL_EMBED_CACHE=str(cache_dir),
        PYTHONPATH=str(REPO),  # dump.py imports tests.eval from the checkout
    )
    env.pop("EVAL_LONGMEMEVAL_PER_TYPE", None)
    if run.get("per_type"):
        env["EVAL_LONGMEMEVAL_PER_TYPE"] = str(run["per_type"])
    return env


def load_model(name, fp32=False):
    import torch
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(name, padding_side="left")
    if torch.cuda.is_available() and fp32:
        # For the few texts fp16 overflows on: fp32 weights across the GPUs,
        # the rest offloaded to CPU memory. Slow, but only for those texts.
        gpus = {i: "11GiB" for i in range(torch.cuda.device_count())}
        model = AutoModel.from_pretrained(
            name, torch_dtype=torch.float32, device_map="auto", max_memory={**gpus, "cpu": "64GiB"}
        )
    elif torch.cuda.is_available():
        model = AutoModel.from_pretrained(name, torch_dtype=torch.float16, device_map="auto")
    else:
        model = AutoModel.from_pretrained(name, torch_dtype=torch.float32)
    model.eval()
    return tokenizer, model


def tokenize(tokenizer, texts, max_len):
    # Qwen3-Embedding's tokenizer appends <|endoftext|> (not eos_token, which is
    # <|im_end|>); keep it on texts cut by truncation too.
    eos = tokenizer.convert_tokens_to_ids("<|endoftext|>")
    ids = []
    for t in texts:
        x = tokenizer(t, add_special_tokens=True, truncation=True, max_length=max_len - 1)["input_ids"]
        if not x or x[-1] != eos:  # Qwen3-Embedding pools the final <|endoftext|> token
            x = x + [eos]
        ids.append(x)
    return ids


def forward(model, **inputs):
    """The model call, with attention forced onto the memory-efficient kernel
    where it exists: the default can pick the math kernel, which builds the full
    heads x L x L score matrix (gigabytes for an 8k-token text on 8B)."""
    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel

    if torch.cuda.is_available():
        try:
            with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):
                return model(**inputs)
        except RuntimeError as exc:
            if "No available kernel" not in str(exc):
                raise
    return model(**inputs)


def embed_batch(tokenizer, model, ids, dim):
    import torch

    device = next(model.parameters()).device
    out = None
    try:
        with torch.no_grad():
            if len(ids) == 1:
                # No padding, no mask: attention takes the memory-efficient causal path.
                out = forward(model, input_ids=torch.tensor(ids, device=device))
            else:
                batch = tokenizer.pad({"input_ids": ids}, padding=True, return_tensors="pt")
                out = forward(model, input_ids=batch["input_ids"].to(device), attention_mask=batch["attention_mask"].to(device))
            last = out.last_hidden_state[:, -1].float()  # left padding: the last position is each text's EOS
    except torch.OutOfMemoryError:
        if len(ids) == 1:
            raise
        out = None
        torch.cuda.empty_cache()
        half = len(ids) // 2  # out of memory: embed the batch in two halves
        return embed_batch(tokenizer, model, ids[:half], dim) + embed_batch(tokenizer, model, ids[half:], dim)
    finite = torch.isfinite(last).all(dim=-1)
    if not finite.all():
        # fp16 overflows on some texts, whatever their length. Redo a bad row
        # alone; one that still overflows comes back as None for the fp32 pass.
        if len(ids) == 1:
            return [None]
        good = torch.nn.functional.normalize(last[:, :dim], dim=-1).cpu().tolist()
        return [g if ok else embed_batch(tokenizer, model, [x], dim)[0] for x, g, ok in zip(ids, good, finite.tolist())]
    vec = torch.nn.functional.normalize(last[:, :dim], dim=-1)
    return vec.cpu().tolist()


def free_gpu():
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass


def embed_fitting(tokenizer, model, text, dim):
    """One text, shortened if the GPU runs out of memory: keep its first half
    (and its final <|endoftext|>) until it fits. Returns (vector, tokens used)."""
    import torch

    while True:
        try:
            return embed_batch(tokenizer, model, [text], dim)[0], text
        except torch.OutOfMemoryError:
            free_gpu()
            if len(text) <= 64:
                raise
            text = text[: len(text) // 2 - 1] + text[-1:]


def run_one(run):
    tag = run["tag"]
    cache_dir = WORK / f"cache-{tag}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = harness_env(run, cache_dir)
    texts_file = WORK / f"texts-{tag}.jsonl"
    dump_file = WORK / "dump.py"
    dump_file.write_text(DUMP)
    subprocess.run(["uv", "run", "python", str(dump_file), str(texts_file)], cwd=REPO, env=env, check=True)

    items = [json.loads(line) for line in texts_file.open()]
    if items:
        import torch

        tokenizer, model = load_model(run["model"])
        ids = tokenize(tokenizer, [i["text"] for i in items], run.get("max_len", 8192))
        order = sorted(range(len(items)), key=lambda i: len(ids[i]))
        db = sqlite3.connect(cache_dir / "embeddings.sqlite3")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT)")
        total = sum(len(x) for x in ids)
        done_tokens, started, pos = 0, time.time(), 0
        deferred, too_long, shortened = [], [], []
        while pos < len(order):
            # Padded batches cost tokens x n and attention L^2 x n; long texts go one at a time.
            n = 1
            while pos + n < len(order):
                longest = len(ids[order[pos + n]])
                if longest * (n + 1) > run["batch_tokens"] or longest * longest * (n + 1) > run.get("attn_budget", 2**24):
                    break
                n += 1
            chunk = order[pos : pos + n]
            try:
                vectors = embed_batch(tokenizer, model, [ids[i] for i in chunk], run["dim"])
            except torch.OutOfMemoryError:  # a single text too long for the GPU
                free_gpu()
                too_long += chunk
                vectors = []
            deferred += [i for i, v in zip(chunk, vectors) if v is None]
            db.executemany(
                "INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)",
                [(items[i]["key"], json.dumps(v)) for i, v in zip(chunk, vectors) if v is not None],
            )
            db.commit()
            pos += n
            done_tokens += sum(len(ids[i]) for i in chunk)
            if pos == len(order) or pos % 500 < n:
                rate = done_tokens / max(time.time() - started, 1e-9)
                left = (total - done_tokens) / max(rate, 1e-9)
                print(f"[{tag}] {pos}/{len(order)} texts, {rate:,.0f} tok/s, about {left / 60:.0f} min left", flush=True)
        for i in too_long:
            vector, text = embed_fitting(tokenizer, model, ids[i], run["dim"])
            if len(text) < len(ids[i]):
                shortened.append((len(ids[i]), len(text)))
            if vector is None:
                ids[i] = text
                deferred.append(i)
            else:
                db.execute("INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)", (items[i]["key"], json.dumps(vector)))
                db.commit()
        del model
        free_gpu()
        if deferred:
            print(f"[{tag}] {len(deferred)} texts overflowed in fp16; embedding them in fp32", flush=True)
            tokenizer, model = load_model(run["model"], fp32=True)
            for i in deferred:
                vector, text = embed_fitting(tokenizer, model, ids[i], run["dim"])
                if len(text) < len(ids[i]):
                    shortened.append((len(ids[i]), len(text)))
                if vector is None:
                    raise RuntimeError(f"non-finite embedding in fp32 too ({len(ids[i])} tokens)")
                db.execute("INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)", (items[i]["key"], json.dumps(vector)))
                db.commit()
            del model
            free_gpu()
        notes = []
        if deferred:
            notes.append(f"{len(deferred)} of {len(items)} texts embedded in fp32 after an fp16 overflow")
        if shortened:
            notes.append(f"{len(shortened)} texts too long for the GPU, shortened (tokens before, after): {shortened}")
        if notes:
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / f"longmemeval-{tag}-notes.txt").write_text("\n".join(notes) + "\n")
            print(f"[{tag}] " + "; ".join(notes), flush=True)
        db.close()

    OUT.mkdir(parents=True, exist_ok=True)
    env["EVAL_LONGMEMEVAL_JSON"] = str(OUT / f"longmemeval-{tag}.json")
    with (OUT / f"longmemeval-{tag}.md").open("w") as md:
        subprocess.run(["uv", "run", "python", "-m", "tests.eval.longmemeval"], cwd=REPO, env=env, stdout=md, check=True)
    print((OUT / f"longmemeval-{tag}.md").read_text())


if __name__ == "__main__":
    for r in json.loads(sys.argv[1]):
        run_one(r)
