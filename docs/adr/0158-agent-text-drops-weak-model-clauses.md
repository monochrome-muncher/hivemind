# The tool text drops two clauses written for weak models

Amends [ADR 0151](0151-whoami-says-a-switch-is-a-new-session.md) decision 1 and [ADR 0148](0148-match-strength-on-search-hits.md) decision 6. Builds on [ADR 0154](0154-the-skill-does-not-repeat-descriptions-or-replies.md) (always-on text does not repeat the server's replies).

Status: Accepted.

## Context

Hivemind's agents run models of at least the capacity of a 26-27B open model and up (ADR 0157). Two clauses of the always-on tool text were written for smaller ones:

- `hive_whoami`'s description ended with a step-by-step instruction ("to act as another agent or fleet, ask the user to start a new session") because Qwen3-8B never loaded the `hivemind-switch` skill (ADR 0151).
- `hive_search`'s description explained `closest: loose`, although the search reply carries that advice as its own `note` (ADR 0148 decision 3).

## Decision

1. `hive_whoami`'s description ends "The key is fixed per session (to switch: hivemind-switch skill)." The fact stays; the how-to lives in the skill.
2. `hive_search`'s description no longer explains `closest: loose`. The reply's `note`, `match` and `similarity` are unchanged.

## Consequences

- The always-on text measures 17,270 bytes, 159 fewer, under the 18,000-byte budget.
- Checked before and after on Claude Opus, Sonnet and Haiku (one run each, the full always-on text, scripted results; rig in the project files, `reviews/2026-11-17-description-trim-rig/`): asked to switch to another profile, every model read the `hivemind-switch` skill, made no `hive_*` call and told the user to start a new session; given a single loose hit for an open question, every model answered generally and named the hit as a loose aside. No difference between the texts.
- A model that does not load skills by their description loses the how-to. Such models are out of scope.
