# A write warns when the entry is not in English

Amends [ADR 0129](0129-entries-in-english-replies-in-the-users-language.md), which left a server-side language check out, and [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes). Adds a row to SPEC §4.1. Refuses, rewrites and supersedes nothing (ADR 0001).

Status: Accepted. Always on, advisory only.

## Context

ADR 0129 put the rule "entries are written in English" in the `hive_write` description, the server instructions and the skill, and left a server check for later "if production shows the rule is not enough". Its worry was that a heuristic would misfire on terse summaries full of names.

A live check on 2026-11-11 tested the rule in OpenCode against Qwen3-14B (run locally; no hosted model was reachable). A user wrote in Norwegian and asked the agent to save a finding:

| Contract text | Entry language (runs) |
|---|---|
| As released (rule in the middle of the `hive_write` description) | Norwegian (2 of 2) |
| Rule moved to the first sentence of the description | Norwegian (1 of 1) |
| Rule also in the `summary` and `body` field descriptions | Norwegian (1 of 1) |
| As released, plus a `not_english` warning in the write reply | Norwegian, then the agent told the user, in Norwegian, that it would rewrite it in English |

The reply rule held: asked in Norwegian about a German entry, the agent answered in Norwegian. A first warning wording that did not mention the reply language made the agent answer in English, so the message names it.

The misfire worry is smaller than feared. A function-word check flags none of the 3,445 English paragraphs in the repository's docs nor any of the eval corpora's entries (the five domains, the golden set and the 480 extension entries).

## Decision

1. **`not_english`** joins the advisory codes. It fires on writes and checks, in every scope, when `summary` and `body` together are not in English.
2. **The check is a word count, no library.** The text is not English when at least 20 of its letters are and most of them are outside the Latin script, or when it has at least three common function words of German, French, Spanish, Italian, Portuguese, Dutch or a Scandinavian language and they outnumber the English function words two to one. Words that are also English words, abbreviations, commands or locale codes (`die`, `was`, `per`, `du`, `os`, `de`, `en`) are left out of the lists. English text that quotes a foreign error stays quiet: its own English words outnumber the quote's.
3. **The message asks for a rewrite, not a withdrawal**: write it again in English, with `supersedes` naming the stored entry, names, quotes and errors as they are, and keep answering the user in their language.
4. **Precision over recall.** A short summary with one or two foreign words ("httpx statt requests") is missed. Languages outside the lists are caught only in a non-Latin script.

## Consequences

- An agent that ignores the description sees the rule again in the write's reply, at the moment it can act on it, with no always-on text added.
- On the 14B model the warning made the agent aware but not act within the same turn. A stronger model is expected to supersede at once, as it does for the other warnings; this was not measured (no hosted model was reachable).
- A false warning costs a second look. The word lists live in `write_warnings.py`; a language agents use that the lists miss is a one-line addition.
