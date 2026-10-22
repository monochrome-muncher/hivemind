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
        gpus = {i: "13GiB" for i in range(torch.cuda.device_count())}
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


def embed_batch(tokenizer, model, ids, dim):
    import torch

    device = next(model.parameters()).device
    out = None
    try:
        with torch.no_grad():
            if len(ids) == 1:
                # No padding, no mask: attention takes the memory-efficient causal path.
                out = model(input_ids=torch.tensor(ids, device=device))
            else:
                batch = tokenizer.pad({"input_ids": ids}, padding=True, return_tensors="pt")
                out = model(input_ids=batch["input_ids"].to(device), attention_mask=batch["attention_mask"].to(device))
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
        tokenizer, model = load_model(run["model"])
        ids = tokenize(tokenizer, [i["text"] for i in items], run.get("max_len", 8192))
        order = sorted(range(len(items)), key=lambda i: len(ids[i]))
        db = sqlite3.connect(cache_dir / "embeddings.sqlite3")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT)")
        total = sum(len(x) for x in ids)
        done_tokens, started, pos = 0, time.time(), 0
        deferred = []
        while pos < len(order):
            # Padded batches cost tokens x n and attention L^2 x n; long texts go one at a time.
            n = 1
            while pos + n < len(order):
                longest = len(ids[order[pos + n]])
                if longest * (n + 1) > run["batch_tokens"] or longest * longest * (n + 1) > run.get("attn_budget", 2**24):
                    break
                n += 1
            chunk = order[pos : pos + n]
            vectors = embed_batch(tokenizer, model, [ids[i] for i in chunk], run["dim"])
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
        del model
        free_gpu()
        if deferred:
            print(f"[{tag}] {len(deferred)} texts overflowed in fp16; embedding them in fp32", flush=True)
            tokenizer, model = load_model(run["model"], fp32=True)
            for i in deferred:
                vector = embed_batch(tokenizer, model, [ids[i]], run["dim"])[0]
                if vector is None:
                    raise RuntimeError(f"non-finite embedding in fp32 too ({len(ids[i])} tokens)")
                db.execute("INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)", (items[i]["key"], json.dumps(vector)))
                db.commit()
            del model
            free_gpu()
            OUT.mkdir(parents=True, exist_ok=True)
            note = f"{len(deferred)} of {len(items)} texts embedded in fp32 after an fp16 overflow"
            (OUT / f"longmemeval-{tag}-notes.txt").write_text(note + "\n")
        db.close()

    OUT.mkdir(parents=True, exist_ok=True)
    env["EVAL_LONGMEMEVAL_JSON"] = str(OUT / f"longmemeval-{tag}.json")
    with (OUT / f"longmemeval-{tag}.md").open("w") as md:
        subprocess.run(["uv", "run", "python", "-m", "tests.eval.longmemeval"], cwd=REPO, env=env, stdout=md, check=True)
    print((OUT / f"longmemeval-{tag}.md").read_text())


if __name__ == "__main__":
    for r in json.loads(sys.argv[1]):
        run_one(r)
