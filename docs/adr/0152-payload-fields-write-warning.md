# A write warns when its payload holds a field of the write itself

Amends [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes). Adds a row to SPEC §4.1. Refuses, rewrites and moves nothing.

Status: Accepted. Advisory only.

## Context

`payload` is free-form structured data (SPEC §4). A weak model can mistake it for a bag of arguments. A live run on 2026-11-13 (written up in `docs/reviews/2026-11-15-plugins-skills-review.md`) had Qwen3-8B call `hive_write` with `kind` and `summary` right and everything else inside `payload`:

```json
{"scope": "fleet", "importance": 3, "sources": [{"type": "session", "ref": "current"}], "tags": ["client", "fleet", "switch"]}
```

The server stored the entry with the default scope, default importance, no sources and no tags, and said nothing. The model believed its tags, sources and scope had been applied, and told its user so.

## Decision

1. **`payload_fields`** joins the advisory codes. It fires in every scope, on writes and on checks, when a top-level key of `payload` names a field of the write itself and that field was left unset:
   - `tags`, `sources`, `supersedes`, `see_also`: the draft has none;
   - `importance`: its source is the default, not the caller;
   - `occurred_at`: the draft has none;
   - `scope`: the value is `self`, `fleet` or `org` and differs from the scope the draft carries (a draft always has a resolved scope, so the value is the signal).
2. **Only unset fields count.** A payload may legitimately hold AWS resource tags while the entry carries its own `tags`; when the argument was set, the key in `payload` is data, not a misplaced argument. The same goes for a `scope` that equals the applied one: nothing was lost.
3. **Exact keys, top level only.** Matching is case-sensitive; nested keys and other names never count.
4. **One warning** lists the fields it found in a fixed order (`scope`, `tags`, `sources`, `supersedes`, `see_also`, `importance`, `occurred_at`) and says what to do: pass them as their own arguments, and if the entry was stored, supersede it with them.

## Consequences

- The run above would have drawn the warning naming `scope`, `tags`, `sources` and `importance` (its 3 is the default, so it was not applied either). A model that reads the reply can supersede the entry with the fields in place; one that does not still leaves the signal for a reviewer.
- No always-on text is added; the correction arrives in the write reply, when the agent can act on it.
- A payload that holds a same-named key on purpose, with the argument unset, gets a warning it can ignore; a false warning costs a second look.
- The rule lives in `write_warnings.py`.
