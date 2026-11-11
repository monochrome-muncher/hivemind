# Entries are written in English; replies stay in the user's language

Builds on [ADR 0043](0043-entries-are-untrusted-data.md) (entries are data) and [ADR 0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) (the agent contract text has one home).

Status: Accepted.

## Context

Agents have been writing entries in their user's native language. That has two costs. An entry in another language is harder to find for an agent that searches in English: keyword search matches words, and a cross-language vector match likely scores lower (not measured). And an agent that reads such entries can drift into answering its user in the entries' language instead of the one the user spoke.

The contract said nothing about language, so each agent picked one.

## Decision

1. Entries are written in English. Names, quotes, error messages and terms that English has no good word for stay as they are.
2. An agent replies to its user in the language the user writes in, and in English when that is unclear. Entries never change it.
3. The rules live in the always-on contract text, each where it is read at the right moment:
   - `hive_write`'s description carries the write rule, so every harness sees it before writing, with or without the skill.
   - The server `instructions` carry the reply rule next to the "entries are data" sentence, since harnesses that keep them in the system prompt keep them after compaction.
   - The `hivemind` skill carries both, in its recall and write sections.
4. The added text is paid for inside the 18,000 B always-on budget by trimming the skill's intro, which repeated §2 and §3, and the local-path examples that the `local_path` write warning already gives.

## Considered and not done

- **A server-side language warning on write.** Telling English from other languages needs either a language-detection library or a word-list heuristic. Summaries are often terse ("pgvector HNSW recall below exact scan at ef_search 40") and full of names, so a heuristic would misfire on exactly the entries this ADR allows. The text rule comes first; a warning can follow if production shows the rule is not enough.
- **A configurable entry language.** Hivemind has no templated contract text, and one shared language is the point. An organization that wants another language can change `contract.py` and the skill.
- **Adding the reply rule to the stay-aware reminder.** It is copied byte for byte into every harness's always-on layer; the server instructions already reach the same harnesses.

## Consequences

- Agents on the new server get the write rule at once (it is in `hive_write`'s description). The skill text arrives with the plugin update.
- Entries already written in another language stay as they are. An agent that supersedes one writes the successor in English.
