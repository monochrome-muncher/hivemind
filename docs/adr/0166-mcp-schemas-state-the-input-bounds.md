# The MCP tool schemas state the input bounds the server checks; refusals say how to fix the call

Production's "Tool refusals per minute, by code" panel (ADR 0161) showed most refusals as `hive_write` `invalid_input`, then `hive_get` `not_found` and `hive_feedback` `invalid_verdict`. The exposed schemas named none of the bounds behind them: `summary` had no length, `kind`, `scope` and `verdict` no value list, `importance` no range, and an omitted `verdict` was sent as `""`. Agents learned the rules from refusals, and some refusals did not say how to fix the call (an unknown kind answered `'note' is not a valid Kind`). We decided to advertise the bounds in the schemas, keep the runtime lenient, and make the refusals say what to change.

Status: Accepted; extends 0105 to the other bounded `hive_write`, `hive_search`, `hive_list` and `hive_feedback` arguments.

## Decision

1. **The schemas state the bounds.** As for `sources` (ADR 0105), a `WithJsonSchema` override advertises `kind` (`fact|insight|decision`, also on the search and list filters), `summary` (`maxLength` 280), `scope` (`self|fleet|org`), `importance` (1 to 5), `see_also` (`maxItems` 5) and `verdict` (`helpful|stale|wrong`). The values come from the domain enums and constants, so schema and checks cannot drift. Schemas are not always-on text (ADR 0154's budget counts the skill, the instructions and the descriptions), so this adds nothing to it.
2. **The runtime stays lenient.** The runtime types stay plain strings and integers: a value outside the schema still reaches the tool's own check and answers the structured refusal, which the metrics count under its code.
3. **Case and padding are forgiven, over MCP only (REST stays exact).** `kind` (also the search and list filter), both `scope`s and `verdict` are matched case-insensitively with surrounding whitespace ignored; entry ids passed to `hive_get`, `hive_feedback`, `supersedes` and `see_also` are trimmed. These are spellings of a valid value, never a different value: no synonyms (`outdated` is not `stale`).
4. **Refusals say how to fix the call.** An unknown kind lists the kinds and says labels go in tags; an overlong summary says to keep one headline and move the details into `body`; an unknown or missing verdict lists the verdicts and what each means; an unknown scope says to omit it for the default; `hive_get` `not_found` and an unknown `see_also` id say to copy the id exactly from a hit or `related`. An unreadable id and an unknown one still get the same answer (ADR 0033).

## Considered options

- **Typed parameters (`Literal`, `constr`)**: the SDK would refuse a bad value itself, as a raw `is_error` text the metrics never see. Rejected for the reason ADR 0105 gives.
- **More skill text**: costs always-on bytes on every session, while the schema reaches the model at the moment it fills the arguments. Rejected.
- **A bounded `field` label on the refusal counter**: would show which field is refused; left out until the panel shows whether this change is enough.

## Consequences

- A client that validates arguments against the schema catches these mistakes before the call; one that does not gets a refusal that names the fix.
- `hive_feedback`'s `verdict` defaults to null instead of `""`; omitting it still answers `invalid_verdict`.
