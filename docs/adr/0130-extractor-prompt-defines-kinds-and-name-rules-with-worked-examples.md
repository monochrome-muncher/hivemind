# The extractor prompt defines the kinds and how to write names, with worked examples

Amends [ADR 0016](0016-write-time-entity-extraction-facets-best-effort-optional-extractor.md) (the fixed extractor prompt). Builds on [ADR 0043](0043-entries-are-untrusted-data.md) (entries are data).

Status: Accepted.

## Context

The entity facet filter matches names exactly (case-insensitive, SPEC §13.3). A facet is only useful across entries if the extractor writes the same thing the same way every time. The first prompt named the six kinds without defining them, asked for "salient" entities without saying what counts, and said nothing about how to write a name. So "Postgres", "PostgreSQL" and "postgres db" became three facets, "k8s" and "Kubernetes" two, and the kind of the same thing changed between entries (a library as `system` in one entry and `organization` in another). It also did not say that the entry text is data.

## Decision

1. The system prompt says which entities to return (specific, named things the entry is about; no generic nouns, dates, amounts, metrics or error messages), and in what order (first mention, most central first when there are more than ten).
2. It says how to write a name: the entity's usual proper name as its own documentation writes it, without articles, possessives, surrounding generic words or version numbers; identifiers such as paths and tickets copied exactly; one item per entity.
3. It defines each kind in one line.
4. It says the entry text is data and any request in it is ignored.
5. Three short worked examples go before the entry as user and assistant turns: one technical, one non-technical, one with no entities. Small models follow examples more reliably than rules.
6. A unit test keeps the prompt in step with the code: every `EntityKind` has a definition line, and every example answer passes the extractor's own validation.

## Considered and not done

- **Structured output (`response_format` with a JSON schema).** Guided decoding would rule out malformed JSON, but OpenAI's schema form needs an object at the root, so the response shape and its validation would change, and not every OpenAI-compatible server accepts the parameter. A server that rejects it would turn every extraction into a failure.
- **Normalising names in code** (for example "auth service" and "auth-service" as one name). That is alias resolution, which SPEC §13.5 keeps out of scope; the prompt asks for the usual name, nothing more.

## Consequences

- Each extraction call sends about 500 more input tokens for the rules and the examples (some 2,000 characters). The output is unchanged in shape and size.
- Entries extracted before this change keep their facets; there is no backfill (SPEC §13.2). Old and new spellings of the same entity can coexist in the pool, so a filter on "Postgres" does not find entries that now carry "PostgreSQL".
- Measured once, on two small local models (Qwen2.5-7B-Instruct and Qwen3-4B-Instruct, 4-bit, temperature 0) over 13 entries from five domains plus one prompt-injection entry, old prompt against new. On the 7B model, malformed JSON went from 2 entries to 0, entities whose kind changed between entries from 4 to 0, and version numbers and generic nouns ("city clerk", "AUC") mostly dropped out. On the 4B model, wrong kinds and generic nouns fell too, and "k8s", "postgres db" and "Stripe payouts" came out as "Kubernetes", "PostgreSQL" and "Stripe". Neither model followed the alias rule every time ("Postgres", "auth service" remained), and neither followed the injected instruction under either prompt. The production model is larger; this was not measured on it.
