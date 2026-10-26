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
# Where the vector caches live, and where to look for caches from earlier runs
# to merge in. On Kaggle the notebook points LME_CACHE into /kaggle/working, so
# a version's output holds the cache, and LME_SEED at /kaggle/input, where an
# earlier version's output appears once it is added as an input.
CACHE = Path(os.environ.get("LME_CACHE", str(WORK)))
SEED = os.environ.get("LME_SEED")
STARTED = time.time()
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# Runs the harness's code to list the (cache key, text) pairs one model needs.
DUMP = r'''
import json, os, sqlite3, sys
from hivemind.config import load_settings
from hivemind.embeddings import build_embedder
from tests.eval.embed_cache import CACHE_FILE, DiskCachingEmbedder
from tests.eval.longmemeval import (VARIANTS, dataset_path, is_abstention, load_questions,
    parse_date, per_type_limit, select, session_draft, variant_config)

settings = load_settings()
cache = DiskCachingEmbedder(build_embedder(settings), os.environ["EVAL_EMBED_CACHE"])
questions = [q for q in select(load_questions(dataset_path()),
             per_type_limit(os.environ.get("EVAL_LONGMEMEVAL_PER_TYPE")))
             if not is_abstention(q)]
# The query text differs only by the instruction, so one config per distinct one.
queries = list({variant_config(v).query_instruction: variant_config(v).embedded_query for v in VARIANTS}.values())
done = {r[0] for r in cache._db.execute("SELECT key FROM vectors")}
seen = set()
with open(sys.argv[1], "w") as out:
    for q in questions:
        items = [("t", query(q["question"])) for query in queries]
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
    """Token ids per text, each a compact int32 array: as Python lists the full
    set's 35M ids take gigabytes of RAM, enough to get the process killed."""
    import numpy as np

    # Qwen3-Embedding's tokenizer appends <|endoftext|> (not eos_token, which is
    # <|im_end|>); keep it on texts cut by truncation too.
    eos = tokenizer.convert_tokens_to_ids("<|endoftext|>")
    ids = []
    for start in range(0, len(texts), 256):
        chunk = texts[start : start + 256]
        for x in tokenizer(chunk, add_special_tokens=True, truncation=True, max_length=max_len - 1)["input_ids"]:
            if not x or x[-1] != eos:  # Qwen3-Embedding pools the final <|endoftext|> token
                x = [*x, eos]
            ids.append(np.asarray(x, dtype=np.int32))
    return ids


def memory():
    """This process's peak RAM and the machine's available RAM, for the log."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    try:
        with open("/proc/meminfo") as f:
            info = dict(line.split(":", 1) for line in f)
        available = int(info["MemAvailable"].split()[0]) / 2**20
    except (OSError, KeyError, ValueError):
        return f"peak RAM {peak:.1f} GiB"
    return f"peak RAM {peak:.1f} GiB, {available:.1f} GiB available"


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


def seed_cache(tag, cache_dir):
    """Merge in the vectors of any earlier run's cache for this tag found under SEED."""
    if not SEED:
        return
    target = cache_dir / "embeddings.sqlite3"
    db = sqlite3.connect(f"file:{target}", uri=True)  # so ATTACH takes a read-only URI
    db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT)")
    for found in sorted(Path(SEED).glob(f"**/cache-{tag}/embeddings.sqlite3")):
        if found.resolve() == target.resolve():
            continue
        db.execute("ATTACH DATABASE ? AS seed", (f"file:{found}?mode=ro&immutable=1",))
        before = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        db.execute("INSERT OR IGNORE INTO vectors SELECT key, vector FROM seed.vectors")
        db.commit()
        db.execute("DETACH DATABASE seed")
        after = db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        print(f"[{tag}] merged {after - before} vectors from {found}", flush=True)
    db.close()


def over_budget(run, grace=0.0):
    """True once the run's time budget (hours since this script started), plus
    `grace` hours, is spent."""
    budget = run.get("time_budget_hours")
    return budget is not None and time.time() - STARTED > (budget + grace) * 3600


def stop_for_time(tag, db, message):
    """Out of time: keep what is cached and say how far the run got. Texts not
    yet cached, including any held back for the fp32 pass or for being too
    long, are listed again by the next run."""
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    OUT.mkdir(parents=True, exist_ok=True)
    message = f"Time budget spent: {message}. Run again with this run's output added as an input to carry on."
    (OUT / f"longmemeval-{tag}-progress.txt").write_text(message + "\n")
    print(f"[{tag}] {message}", flush=True)
    return False


def run_one(run):
    """Embed what the cache lacks, then run the harness. Returns False if the
    time budget ran out first: the cache keeps what was done, and running
    again (with that cache) carries on from there."""
    tag = run["tag"]
    cache_dir = CACHE / f"cache-{tag}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    seed_cache(tag, cache_dir)
    env = harness_env(run, cache_dir)
    texts_file = WORK / f"texts-{tag}.jsonl"
    dump_file = WORK / "dump.py"
    dump_file.write_text(DUMP)
    subprocess.run(["uv", "run", "python", str(dump_file), str(texts_file)], cwd=REPO, env=env, check=True)

    keys, texts = [], []
    for line in texts_file.open():
        item = json.loads(line)
        keys.append(item["key"])
        texts.append(item["text"])
    if run.get("max_new_texts") is not None and len(keys) > run["max_new_texts"]:
        # A rerun meant to reuse an earlier run's vectors, without them attached.
        raise SystemExit(
            f"[{tag}] {len(keys)} texts to embed, more than max_new_texts ({run['max_new_texts']}): "
            "the earlier run's cache is missing. Attach that run's output as an input and run again."
        )
    if keys:
        import numpy as np
        import torch
        from transformers import AutoTokenizer

        # Tokenized before the model loads, and the texts dropped, to keep RAM low.
        ids = tokenize(AutoTokenizer.from_pretrained(run["model"]), texts, run.get("max_len", 8192))
        del texts
        gc.collect()
        print(f"[{tag}] {len(ids)} texts tokenized; {memory()}", flush=True)
        tokenizer, model = load_model(run["model"])
        print(f"[{tag}] model loaded; {memory()}", flush=True)
        order = sorted(range(len(keys)), key=lambda i: len(ids[i]))
        db = sqlite3.connect(cache_dir / "embeddings.sqlite3")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT)")
        total = sum(len(x) for x in ids)
        done_tokens, started, pos = 0, time.time(), 0
        deferred, too_long, shortened = [], [], []
        while pos < len(order):
            if over_budget(run):
                break
            # Padded batches cost tokens x n and attention L^2 x n; long texts go one at a time.
            n = 1
            while pos + n < len(order):
                longest = len(ids[order[pos + n]])
                if longest * (n + 1) > run["batch_tokens"] or longest * longest * (n + 1) > run.get("attn_budget", 2**24):
                    break
                n += 1
            chunk = order[pos : pos + n]
            try:
                vectors = embed_batch(tokenizer, model, [ids[i].tolist() for i in chunk], run["dim"])
            except torch.OutOfMemoryError:  # a single text too long for the GPU
                free_gpu()
                too_long += chunk
                vectors = []
            deferred += [i for i, v in zip(chunk, vectors) if v is None]
            db.executemany(
                "INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)",
                [(keys[i], json.dumps(v)) for i, v in zip(chunk, vectors) if v is not None],
            )
            db.commit()
            pos += n
            done_tokens += sum(len(ids[i]) for i in chunk)
            if pos == len(order) or pos % 500 < n:
                rate = done_tokens / max(time.time() - started, 1e-9)
                left = (total - done_tokens) / max(rate, 1e-9)
                print(f"[{tag}] {pos}/{len(order)} texts, {rate:,.0f} tok/s, about {left / 60:.0f} min left; {memory()}", flush=True)
        if pos < len(order) or over_budget(run):
            return stop_for_time(
                tag, db, f"{pos} of {len(order)} texts ({done_tokens:,} of {total:,} tokens) done this run, "
                f"{len(deferred) + len(too_long)} of them held back for a later pass"
            )
        # The passes below are slow but short; they get an hour past the budget.
        for i in too_long:
            if over_budget(run, grace=1.0):
                return stop_for_time(tag, db, "in the pass for texts too long for the GPU")
            vector, text = embed_fitting(tokenizer, model, ids[i].tolist(), run["dim"])
            if len(text) < len(ids[i]):
                shortened.append((len(ids[i]), len(text)))
            if vector is None:
                ids[i] = np.asarray(text, dtype=np.int32)
                deferred.append(i)
            else:
                db.execute("INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)", (keys[i], json.dumps(vector)))
                db.commit()
        del model
        free_gpu()
        if deferred:
            print(f"[{tag}] {len(deferred)} texts overflowed in fp16; embedding them in fp32", flush=True)
            tokenizer, model = load_model(run["model"], fp32=True)
            for i in deferred:
                if over_budget(run, grace=1.0):
                    return stop_for_time(tag, db, "in the fp32 pass")
                vector, text = embed_fitting(tokenizer, model, ids[i].tolist(), run["dim"])
                if len(text) < len(ids[i]):
                    shortened.append((len(ids[i]), len(text)))
                if vector is None:
                    raise RuntimeError(f"non-finite embedding in fp32 too ({len(ids[i])} tokens)")
                db.execute("INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)", (keys[i], json.dumps(vector)))
                db.commit()
            del model
            free_gpu()
        notes = []
        if deferred:
            notes.append(f"{len(deferred)} of {len(keys)} texts embedded in fp32 after an fp16 overflow")
        if shortened:
            notes.append(f"{len(shortened)} texts too long for the GPU, shortened (tokens before, after): {shortened}")
        if notes:
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / f"longmemeval-{tag}-notes.txt").write_text("\n".join(notes) + "\n")
            print(f"[{tag}] " + "; ".join(notes), flush=True)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"longmemeval-{tag}-progress.txt").unlink(missing_ok=True)
    env["EVAL_LONGMEMEVAL_JSON"] = str(OUT / f"longmemeval-{tag}.json")
    with (OUT / f"longmemeval-{tag}.md").open("w") as md:
        subprocess.run(["uv", "run", "python", "-m", "tests.eval.longmemeval"], cwd=REPO, env=env, stdout=md, check=True)
    print((OUT / f"longmemeval-{tag}.md").read_text())
    return True


if __name__ == "__main__":
    for r in json.loads(sys.argv[1]):
        if not run_one(r):
            break  # out of time; later runs would only be cut short too
