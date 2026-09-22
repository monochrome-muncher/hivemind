# The embedded-text budget is counted in whitespace words, not model tokens

Every entry is embedded from `summary` + a bounded prefix of `body`
(SPEC §7), and — since ADR 0016 — the entity extractor reads that
*same* bounded text (SPEC §13.1). Until now the bound was **2048
characters** (`HIVEMIND_EMBEDDING_PREFIX_CHARS`), with a code comment
calling it "≈ 512 tokens".

Two things were wrong with that.

* **The knob was inert on the embedding side.** `OpenAICompatEmbedder`
  had no `prefix_chars` parameter at all: it called
  `embeddable_text(summary, body)` and always got the function default,
  so `HIVEMIND_EMBEDDING_PREFIX_CHARS` changed entity extraction and
  did **nothing** to embedding. ADR 0016's stated invariant — the
  extractor and embedder read the same bounded text, in lockstep —
  therefore broke silently the moment anyone moved the value off its
  default. Nothing tested it.
* **Characters are the wrong unit for what the bound is for.** The
  bound exists to cap how much of a long body dilutes one vector (and,
  post-ADR-0016, how many tokens an extractor LLM call pays for). Both
  of those are *token* quantities. Characters-per-token varies by 2–3x
  between English prose (~4 chars/token) and code or JSON (~1.5–2
  chars/token, punctuation and identifiers fragment heavily) — so a
  character budget is loosest exactly where agents write most: pasted
  code, stack traces, config blobs.

## Decision

1. **The budget is counted in whitespace-delimited words.** The setting
   is renamed `HIVEMIND_EMBEDDING_PREFIX_TOKENS`
   (`Settings.embedding_prefix_tokens`), default **2000**, and
   `embeddable_text(summary, body, prefix_tokens)` truncates the body to
   its first `prefix_tokens` runs of non-whitespace characters. The old
   name is **deleted** — nothing is deployed, so there is no alias and
   no back-compat shim.

2. **A "prefix token" is a whitespace word, explicitly NOT a model
   tokenizer's token.** This is the deliberate deviation this ADR
   exists to record, because the obvious "fix" for a future reader is to
   reach for `tiktoken`. We do not, for two reasons:
   * **The applicable tokenizer is unknowable at write time.** The
     embeddings endpoint is operator-configured and pluggable (ADR
     0005): OpenAI, a self-hosted vLLM model, Ollama. Their tokenizers
     differ. There is no single correct tokenizer for this code to use.
   * **A vendored tokenizer would be a dependency that is wrong for
     some deployments.** Adding `tiktoken` would give a *precise* count
     against a BPE vocabulary that a self-hosted Qwen or E5 deployment
     does not use — false precision plus a real dependency, plus a
     native wheel in an air-gapped build. A whitespace word is a
     tokenizer-independent, stable, dependency-free proxy that is within
     a constant factor of every tokenizer in use.

3. **The cut preserves the body's original spacing.** The
   implementation finds the end offset of the last word inside the
   budget and slices the original string there. It does **not** split
   on whitespace and re-join on single spaces: that would count the same
   budget but flatten newlines, blank lines and indentation, destroying
   the paragraph structure of exactly the long-form `insight` entries
   the bound is there to bound. A body inside the budget is returned
   byte-for-byte unchanged.

4. **The budget is threaded, and the lockstep is pinned by a test.**
   `OpenAICompatEmbedder.__init__` takes a keyword-only `prefix_tokens`
   (mirroring `OpenAICompatExtractor`), `from_settings` passes
   `settings.embedding_prefix_tokens`, and a test builds *both*
   components from one `Settings` object and asserts the text on the
   wire is byte-identical — and that it is really bounded, so a
   regression where both sides ignore the setting cannot pass by
   agreeing on the unbounded text.

## Why 2000, and what it is bounding

A whitespace word is roughly **1.3 model tokens** across common English
BPE vocabularies, so 2000 words ≈ **2600 model tokens**. That is far
under the limits of the endpoints in use — 8191 tokens for OpenAI
`text-embedding-3-*`, 32k for the Qwen3-Embedding class of self-hosted
models.

**This bound therefore does not exist to respect a model limit.** It
exists to control:

* **Dilution** — one vector represents the whole text; a 20-page body
  averaged into 1024 dimensions retrieves worse than its first section
  does, so more text is not monotonically better.
* **Cost** — ADR 0016 made the same text the extractor's LLM input, at
  one chat completion per write.

The old bound was 2048 characters ≈ 400 words ≈ 520 model tokens. The
new one is ~5x that in words and ~6x in characters for typical prose.

## Consequences

* **`HIVEMIND_EMBEDDING_PREFIX_CHARS` no longer exists.** Deployments
  setting it must rename it; an unrecognised `HIVEMIND_*` var is
  ignored by pydantic-settings (`extra="ignore"`), so a stale name
  silently falls back to the 2000-word default rather than failing —
  the rename is called out in `config/.env.example` and the k8s
  ConfigMap, both updated here.
* **The extractor's LLM input per write grows by roughly 6x** versus
  the old 2048-character bound, because ADR 0016 has it read this exact
  text. That is the main cost of this change and it is accepted
  deliberately: extraction is optional (unset endpoint ⇒ off, zero
  cost) and the input is still ~2600 tokens, small for a chat call.
  A deployment that wants the old economics sets
  `HIVEMIND_EMBEDDING_PREFIX_TOKENS` lower — and now that actually
  moves *both* sides, which was the point.
* **Embedding a long entry now sends ~5x more text** to the embeddings
  endpoint than it did. The vector dimension is unchanged (ADR 0015),
  so only request size and endpoint-side compute change.
* **Existing entries are not re-embedded.** Entries written under the
  character bound keep the vector they have; changing the bound is not
  a re-embedding trigger (the ADR 0005 re-embedding migration is the
  named procedure if an operator wants uniformity).
* **One very long word is one prefix token.** A 5000-character
  base64 blob with no whitespace passes the budget whole, where a
  character bound would have cut it. Payload-shaped bodies belong in
  `payload`, not `body`; if this ever bites, the fix is a belt-and-braces
  character ceiling on top of the word budget, not a tokenizer.
* **ROADMAP §4.2 is narrowed, not closed.** This settles *how much and
  in what unit*. *What* goes into the embedded text (summary + body
  prefix, versus including tags / kind / entities) stays open, and
  whether to **chunk** at all is explicitly out of scope — SPEC §4.1
  commits to one vector per entry, so chunking is a retrieval-shape
  change that needs its own ADR.
