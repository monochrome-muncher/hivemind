# Always-on text trim, 2026-11-15

Baseline: main at dba48fd (2.27.0). Question from Monochrome: can the always-on text be trimmed in a way that matters, without dropping an important instruction or making agents worse?

## Answer

Yes, by about 5%. The always-on text goes from 17,956 to 17,107 bytes (849 freed) and no rule is dropped. It does not change how agents behave: the saving is about 210 of 4,500 tokens per session. What it gives is room. The last rules cost 80 to 250 bytes each, and only 44 bytes were free.

| Part | Before | After |
|---|---|---|
| `hivemind` skill | 9,680 | 9,343 |
| Server `instructions` | 1,295 | 1,239 |
| Ten tool descriptions | 6,601 | 6,145 |
| Reminder | 380 | 380 |
| Total | 17,956 | 17,107 |

## What was cut, and what each cut risks

The rule behind the cuts is in [ADR 0154](../adr/0154-the-skill-does-not-repeat-descriptions-or-replies.md).

| Cut | Where the rule still is | Risk |
|---|---|---|
| Skill: `upstream_changed` re-check line | `hive_search` description | None where the skill loads, since the description loads too |
| Skill: exceptions to the English rule, the list of kinds | `hive_write` description; the `not_english` warning | Same |
| Skill: `hive_whoami` row "Full participation" | It asked for nothing | None |
| Skill: pointer to the `near_duplicate` warning | The warning gives its three readings (`services/write_warnings.py`) | None |
| Skill: local-path how-to (the rule stays) | The `local_path` warning | An agent that skips `check: true` reads the how-to only after storing |
| Skill: "supersede only a chain's current head" | The `supersede_denied` message (`services/errors.py`); the skill keeps what to do | None |
| `hive_register`: walk-through of the replies | The reply's `message` for pending, active and revoked, and the `name_conflict` message (`services/access.py`) | None; the setup skill has the flow too |
| `hive_write`: "an explicit out-of-permission scope is rejected" | The `permission_denied` reply names the trust level and the scope | None |
| `hive_write`: "don't retry in a loop" | Moved into the MCP `embedding_unavailable` message | None; it now arrives with the error |
| `hive_get`, `hive_withdraw`, `hive_feedback`: "what you may not read" clauses | Nothing to act on: an unreadable entry and an unknown id answer the same on purpose | An agent may wonder why a known id answers `not_found` |
| Server `instructions`: shorter org-key sentence | `hive_whoami`, the setup skill | None |

Not cut: the server `instructions` repeat the skill's core (1,239 bytes), but they are the only rules some harnesses keep after compaction. The #456 fixes, scope by evidence, `check: true` before writing, the match and loose-note text and the `alternative` exception are untouched.

Possible later: the foreign-fleet rule in `hive_search`'s description (about 330 bytes) could become a note on replies that hold a foreign hit. It needs a new reply field.

## Check before and after

**Rig.** One in-memory Hivemind per run behind the real MCP server (`build_server`), so tool descriptions, schemas, warnings and errors are the real ones, built once from main and once from this branch. Seeded with a fleet, the TRUNCATE incident and its follow-up decision, and a pinned backup decision. Each agent got what a harness loads (server instructions, reminder, skill, the other skills on offer, the tool list) and called the tools for real. Every call was logged. Rig: project files `reviews/2026-11-15-always-on-trim-rig/`.

**Scenarios.** The known failure cases: the general TRUNCATE question, a request to switch to a client profile and save a decision there, a decision to record with tags, a source and an importance (payload stuffing), a tested finding plus an idea in one message (scope sorting), an untested alternative to record for the team, and in all the writing ones whether the draft was checked with `check: true` first.

**Claude models (Haiku twice, Sonnet once, per scenario and version; 30 runs, plus 5 Haiku runs on the final wording).**

| Scenario | Passes when | Before | After |
|---|---|---|---|
| General TRUNCATE question | General answer first, the incident as a labelled aside | 3 of 3 | 4 of 4 |
| Switch request | Nothing written or pinned, says a new session is needed | 3 of 3 | 4 of 4 |
| Payload stuffing | Tags, source and importance as arguments, empty `payload` | 3 of 3 | 4 of 4 |
| Scope sorting | Finding to the fleet, idea to `self` | 3 of 3 | 4 of 4 |
| Alternative | Fleet entry tagged `alternative`, linked to the rival, says how to test | 3 of 3 | 4 of 4 |
| `check: true` before the write | First `hive_write` is a check | Haiku 6 of 6, Sonnet 2 of 3 | Haiku 9 of 9, Sonnet 2 of 3 |

No scenario changed. Sonnet skipped the check in the scope scenario with both texts, so that is not from the trim; it searched first both times.

**Local Qwen3-4B (Q4_K_M, CPU, its own chat template, thinking on; one run per scenario and version, two for the old TRUNCATE case).** Qwen3-8B and 14B do not fit this container at a usable speed. With thinking off the model made no valid tool call at all.

| Scenario | Before | After |
|---|---|---|
| General TRUNCATE question | General answer, no search | General answer, no search |
| Switch request | Called a tool named `hivemind-switch` (not found), wrote the decision to the current fleet and said it was in the client's | Same call, wrote the decision to the current fleet, said the switch skill was not available |
| Payload stuffing | Tags and importance as arguments | Tags, importance and source as arguments |
| Scope sorting | Finding and idea both to the fleet | Finding to the fleet, idea to `self` |
| Alternative | Fleet entry, no `alternative` tag, no link | `self` entry, no `alternative` tag, no link |
| `check: true` before the write | Never | Never |

The 4B model fails most of these with either text: it never calls `hive_whoami`, never searches and never checks a draft, so most of the skill does not reach its behaviour. Nothing got worse with the trimmed text. The two differences (scope sorting passes, the alternative lands in `self`) are single runs and within its noise. The switch failure is the one ADR 0151 addresses through `hive_whoami`'s description, which this model does not call; it is the same before and after.

## Review

A reviewer (Opus) read every cut against the server code. It confirmed the rules survive and asked for four wording fixes, which are in: keep "(self or fleet)" in `hive_write`'s description for sessions without the skill, a clearer English line and `supersede_denied` line in the skill, and "keep the entry locally" in the error message.
