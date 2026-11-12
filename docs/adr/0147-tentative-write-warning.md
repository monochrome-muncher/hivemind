# A shared write warns when it reads as an untested idea

Amends [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes) and backs [ADR 0146](0146-scope-by-evidence.md) (scope by evidence) with a check in the write reply, the way [ADR 0134](0134-not-english-write-warning.md) backs the English rule. Adds a row to SPEC §4.1. Refuses, rewrites and moves nothing.

Status: Accepted. Advisory only.

## Context

ADR 0146 told agents to keep ideas, brainstorms and plans in `self` until tested. A live check on 2026-11-12 showed Claude Opus, Sonnet and Haiku follow it from the text alone. Qwen3-14B in OpenCode did not: under the old text and the new one, a user who reported a verified fix and added "maybe someday we should move it to Celery, I don't know" got a shared `decision` entry whose body ended "Consider moving the job to a queue like Celery in the future". That is the shape of the reported incident: a passing idea, written for every agent in the fleet, filed as a decision.

## Decision

1. **`tentative`** joins the advisory codes. It fires on `fleet` and `org` writes and checks, never on `self`, when `summary` or `body` contains a phrase that proposes rather than reports: "what if", "maybe we" (or you, I, they, someday, later), "perhaps we", "we could/might/should try" (or consider, move, switch, drop, use, add, replace, explore, rewrite, just), "could/might be worth", "worth trying" (or exploring, considering, a try, investigating, prototyping), "consider moving" (or switching, using, adding, replacing, dropping, migrating, trying, rewriting), "consideration is given", "someday", "brainstorm…", "thinking out loud", "just an idea", "random thought/idea", or "idea:".
2. **Precision over recall.** A bare "might", "could", "probably" or "in the future" does not count: findings use them for conditions ("might be the S3 timeout; could not reproduce") and lessons ("to avoid this in the future"). An idea worded as a plain claim is missed.
3. **Entries tagged `alternative` or `open-question` are left alone**: they are untested by design and belong in the fleet (ADR 0146 decision 4).
4. **The message names the phrase** and says what to do: keep ideas in `self` until tested, then supersede the self note with a fleet entry; for a finding that carries a passing idea, leave the idea out or move it to a `self` note that links to the entry; if it was stored, supersede it without the idea.

## Consequences

- No eval-corpus entry (the domain corpora and the golden set, 168 entries) and none of seven realistic findings, dead ends and decisions written in the live check are flagged. Of 3,810 paragraphs in the repository's Markdown (proposals left out), the only ones flagged discuss brainstorming itself, in ADR 0146, this ADR, the skill and the 2026-11-12 review. 14 of 15 hand-written and live-check idea entries are flagged; the miss words its idea as a claim ("drop the staging layer ... probably nobody would notice").
- An agent that ignores the scope rule sees it again in the write reply, at the moment it can act on it, with no always-on text added.
- A false warning costs a second look. The phrases live in `write_warnings.py`.
