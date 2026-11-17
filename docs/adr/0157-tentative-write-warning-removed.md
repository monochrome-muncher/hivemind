# The `tentative` write warning is removed

Supersedes [ADR 0147](0147-tentative-write-warning.md). Amends [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes) and SPEC §4.1. [ADR 0146](0146-scope-by-evidence.md) (scope by evidence) stands as text.

Status: Accepted.

## Context

ADR 0147 added `tentative`, an advisory warning on `fleet` and `org` writes whose text proposes rather than reports ("what if", "maybe we", "worth trying"), as a net under the scope-by-evidence rule for weaker models. Its own live checks showed it did not catch anything that mattered:

- Claude Opus, Sonnet and Haiku kept ideas out of the fleet from the text alone, before the warning existed.
- Qwen3-14B, the model it was built for, drew the warning and stored the entry unchanged.

Hivemind's agents run models of at least the capacity of a 26-27B open model (Gemma 4 26B, Qwen 3.8 27B) and up; smaller agent models are out of scope. A phrase list that only models below that floor would need, and that they ignored, is upkeep with no return.

## Decision

1. `tentative` is no longer a warning code. A write whose text reads as an idea gets no warning for it.
2. The scope rule stays where strong models read it: the skill's "Scope by evidence" and the server instructions' "keep untested ideas in self".
3. `not_english` (ADR 0134) stays. Unlike a short test, a long session full of foreign-language tool output (chat, tickets) can pull any model toward that language, and a strong model acts on the warning.
4. Agent-text checks are run on Claude Opus, Sonnet and Haiku, plus a 26-27B open model when one can run, not on smaller local models.

## Consequences

- A write reply carries one code fewer. No agent text named the code, so no skill or description changes.
- A brainstorm that a model writes to the fleet is caught by readers (`stale`/`wrong` feedback, the `unverified` tag of ADR 0156, move to self of ADR 0145), not by the server.
- The "next lever is a write warning" notes in ADRs 0146 and 0156 no longer apply.
