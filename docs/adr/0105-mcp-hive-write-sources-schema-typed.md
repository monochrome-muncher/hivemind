# The MCP `hive_write` `sources` schema is typed, validated leniently

The MCP `tools/list` answer for `hive_write` exposed `sources` as an untyped open map (an array of objects with `additionalProperties: string`, no required keys, no enum), because the schema is generated from the tool's `sources: list[dict[str, str]]` signature and the `hive_write` description never named the field. Every agent writing a sourced entry had to guess the shape and pay one or two `invalid_input` round-trips to learn it. We decided to expose a **typed** `sources` schema while keeping the **lenient** runtime validation.

Status: Accepted.

## Context

SPEC §4.1 already defines `sources` as `{type: path|url|session|other, ref}`, and the REST surface types it (`SourceModel`: `type` enum + required `ref`). Only the MCP surface drifted, exposing an open map. An agent (observed in the home fleet) writing `sources: [{text, url}]` was rejected, then `[{text, type, url}]` was rejected, before `[{ref, type, url}]` was accepted — the extra `url` key silently ignored. The server's `invalid_input` messages are clear enough to self-correct from, so the cost is a guaranteed extra round-trip per sourced entry, not a failure.

## Decision

1. **The exposed schema is typed, derived from the domain.** `hive_write`'s `sources` parameter carries a `WithJsonSchema` override whose items schema is `{type: <enum from `SourceType`>, ref: string}`, `required: [type, ref]`, `additionalProperties: false`. The enum values come from `SourceType`, so the schema and the domain cannot drift on the type vocabulary. `compact_schema` collapses the `anyOf [array, null]` to a plain array (the argument is optional and not in `required`).

2. **The runtime stays lenient.** The signature's *runtime* type remains `list[dict[str, str]]`, so the MCP SDK lets a loose object through and the tool body's `_parse_sources` still answers a malformed source with the structured `{"error": {"code": "invalid_input", ...}}` envelope — the same clear, self-correcting messages as before. A client that sends `null` keeps working.

3. **The `hive_write` description names the field.** One line in `DESC_WRITE` documents `sources` as `[{ref, type}]` with the `type` vocabulary, that a URL's `ref` is the URL itself, and that entry ids belong in `see_also`, not `sources`.

4. **Annotation resolution.** `register_tool` now resolves the verb's string annotations in the verb's own namespace (via `get_type_hints`) before storing them on the closure's `__signature__`. The MCP SDK reads that `__signature__` without re-evaluating string annotations, so an annotation that names a verb-module symbol (the schema constant) would otherwise stay an unresolvable forward reference.

## Considered options

- **A typed model in the signature** (`list[Source]`): cleaner types, but the MCP SDK would then validate at its own layer and answer a malformed source as a raw `is_error` text result (`Error executing tool ...: 1 validation error ...`) instead of the structured `invalid_input` envelope. That changes the error shape agents read and self-correct from — the very clarity this fix relies on — for a discoverability gain the schema override already provides. Rejected.
- **Leave the open map and only document it in the description:** removes the guessing for agents that read descriptions, but a client that validates against the (still open) schema would not catch a wrong shape, and the schema would keep disagreeing with the REST surface and SPEC §4.1. The schema override is the same effort and makes the two surfaces agree.

## Consequences

- The MCP and REST surfaces now expose the same `sources` shape, matching SPEC §4.1.
- A well-behaved client that validates against the schema catches a wrong `type` or a missing `ref` before the call; a client that does not still gets the structured `invalid_input`.
- `additionalProperties: false` means a client sending an extra key (e.g. `url`) gets a schema mismatch if it validates, rather than a silent ignore. The server itself still ignores extra keys at runtime.
- Any future tool whose signature annotation names a module-level symbol relies on the `register_tool` resolution step; a new such tool needs no extra wiring.
