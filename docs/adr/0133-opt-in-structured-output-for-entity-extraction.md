# Entity extraction can ask the endpoint for structured output (opt-in)

Amends [ADR 0016](0016-write-time-entity-extraction-facets-best-effort-optional-extractor.md) (the extractor call) and [ADR 0130](0130-extractor-prompt-defines-kinds-and-name-rules-with-worked-examples.md), which listed structured output as considered and not done.

Status: Accepted.

## Context

The extractor asks a chat model for a JSON array and validates it all-or-nothing (SPEC §13.1). When the model writes broken JSON, the entry lands without facets. ADR 0130 cut this on the small models it measured, but did not remove it. Most OpenAI-compatible servers can constrain decoding to a JSON schema (`response_format` with `type: json_schema`): vLLM, llama.cpp's server, Ollama and OpenAI itself. A model decoding under a schema cannot produce malformed JSON or an unknown kind.

ADR 0130 left it out for two reasons. OpenAI's strict schema mode needs an object at the root, so the answer's shape changes. And a server that does not take the parameter answers 400, which would turn every extraction into a failure.

## Decision

1. A setting, `HIVEMIND_EXTRACTOR_STRUCTURED_OUTPUT`, off by default. Off, the request and the parsing are exactly as before.
2. On, the request carries `response_format: {type: json_schema, json_schema: {name: entities, strict: true, schema}}`. The schema is an object with one field, `entities`: an array of at most 10 `{name, kind}` objects, `kind` limited to the six kinds, no other fields. The name has no length limit in the schema, because a grammar would cut a long name short instead of failing; validation still rejects names over 128 characters.
3. On, the prompt and its worked examples ask for `{"entities": [...]}` instead of a bare array, so the instructions and the constraint agree. Only the sentence about the response format and the empty answer change. The answer is accepted as that object or, from a server that ignores the parameter, as a bare array; any other object fails.
4. If the endpoint answers 400 or 422 to a request with `response_format`, the extractor sends the same request once more without it. If that succeeds, the extractor stops sending the parameter for the rest of the process and logs one warning. If it fails too, the failure stands and nothing is remembered, because the 400 was about something else.
5. `hivemind-reextract` builds its extractor from the same settings, so it follows the setting.

## Considered and not done

- **On by default.** Not every deployment's endpoint supports it, and the fallback costs one extra call per process. Operators who know their server turn it on.
- **Probing the endpoint at startup.** It would need a model call before the service is ready, and a failed probe would have to be retried anyway. Learning from the first real call is simpler.
- **vLLM's own `guided_json` parameter.** It works only on vLLM; `response_format` works there and on the other servers.

## Consequences

- With an endpoint that supports it, extraction no longer fails on malformed JSON, unknown kinds or more than ten entities. Names are still the model's choice: the schema does not make the alias rule (ADR 0130) stick.
- The first extraction against a server that rejects the parameter takes one extra, fast round trip; later ones are as before.
- A server that accepts `response_format` but ignores it gets the object-shaped prompt with no constraint, which is as good as the plain prompt.
- llama-cpp-python's own server answers a `json_schema` response format with 500, not 400, so the extractor does not fall back there and every extraction fails. Leave the setting off with that server; llama.cpp's `llama-server` takes it.
- Checked once against llama.cpp's `llama-server` with Qwen2.5-7B-Instruct (4-bit, CPU, temperature 0) over the 14 entries ADR 0130 used. The server constrained the output (a request asking for a haiku came back as schema JSON). All 14 extractions passed validation with the setting off and on, so this model gave no malformed JSON to remove. With it on, 9 of 14 answers were the same and 5 differed, mostly by an extra generic item ("Q3", "2025", "ValueError"); one kind changed ("city clerk" as person instead of organization). The fallback on 400 or 422 is covered by unit tests only; no endpoint here rejected the parameter that way.
