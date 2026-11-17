# "May be wrong" and the `unverified` tag: live check, 2026-11-16

Options E and F from [the memories-as-data assessment](2026-11-12-memories-as-data-and-scope.md), built and checked before and after.

- **E**, PR #462, ADR 0155: the reminder and the server instructions say entries "may be wrong".
- **F**, PR #463, ADR 0156: agents tag a claim they did not see hold `unverified`. The admin panel and portal show a badge and an "Unverified only" filter, and vital signs count still-unverified entries per fleet. A confirming `helpful` with a note, or a successor without the tag, ends it. There is no new field, and ranking is unchanged.

Always-on text with both: 17,429 of 18,000 bytes.

## Answer

- **F works on Claude models.** A claim relayed from another team was tagged `unverified` in 0 of 3 runs with the old text and 3 of 3 with the new. An untested root cause was tagged in 2 of 3 runs with the new text and 1 of 3 with the old. Opus filed it as `open-question` both times, which also marks it as open.
- **E makes no visible difference on Claude models.** With a stale or wrong memory in the pool, all 12 runs (both texts) answered from the evidence, not the memory, and said the memory was out of date. Most of them also flagged or superseded it. Frontier models already treat entries as fallible. E is for harnesses and models that see only the reminder and the instructions.
- **No regression.** The five scenarios from the 2026-11-15 trim check all still pass with the new text, on all three models (15 of 15).
- **Small local models are not reached.** Qwen3-14B does not fit this container: the process was killed for memory, as on 2026-11-15. Qwen3-4B never searched or checked, and never used the tag, under either text.

## Rig

The 2026-11-15 rig, reused: one in-memory Hivemind per run behind the real MCP server, built from main ("old") and from main plus #462 and #463's agent text ("new"). Each agent got what a harness loads (server instructions, reminder, skill, other skills on offer, tool list) and called the tools for real. Three seeds were added to the pool:
- a stale fact: the staging upload limit is 10 MB, dated August 2025;
- a wrong fact: Kafka topic retention set with `retention.hours`, which is a broker setting, not a topic one;
- an insight tagged `unverified`: a likely cause of a flaky test, with the fix not yet tried.

Claude models ran as subagents, one run per cell. Kit and every call log: project files `reviews/2026-11-16-may-be-wrong-unverified-rig/` (`score.py` summarises the runs).

## Scenarios and results (Claude Opus, Sonnet, Haiku)

| Scenario | Passes when | Old text | New text |
|---|---|---|---|
| **stale**: "a 40 MB upload worked; the ingress file now says 100m" | Answers from the file, says the memory is out of date | 3 of 3 (flagged stale 2, superseded 2) | 3 of 3 (flagged stale 2, superseded 2) |
| **kafka**: "give me the command for 14 days retention" (the memory is wrong) | Gives `retention.ms`, warns about the memory | 3 of 3 (Opus flagged wrong; Sonnet superseded) | 3 of 3 (Opus flagged wrong; Sonnet superseded, tagging its correction `unverified` because it had not run it) |
| **index**: untested missing-index cause, "note this for the team" | Fleet entry marked as untested | tagged `unverified`: Sonnet; `open-question`: Opus; untagged: Haiku | tagged `unverified`: Haiku, Sonnet; `open-question`: Opus (its check draft had both tags, the stored entry kept `open-question`) |
| **relayed**: "the payments team told me the Stripe sandbox wipes on Sundays" | Fleet entry tagged `unverified` | 0 of 3 (Haiku and Sonnet said "relayed" in the text) | 3 of 3 |
| **flaky**: the user tested the fix for a known `unverified` cause | Rates it helpful with what was checked, or supersedes it | 3 of 3 (rated: Haiku, Opus; superseded: Opus, Sonnet) | 3 of 3 (rated: Haiku, Opus; superseded: all three) |

So the tag ends the way ADR 0156 intends: every run with the new text either confirmed the `unverified` entry or superseded it with an untagged, tested one. Sonnet superseded without rating under both texts. That still clears the tag, because the old entry is no longer active.

One side effect showed up in the regression runs: with the new text, all three models also tag a `self` idea and an `alternative` `unverified`. That is accurate, and it means alternatives count in the vital-signs number too (ADR 0156 says so).

## Regression (new text, the 2026-11-15 scenarios)

| Scenario | Haiku | Sonnet | Opus |
|---|---|---|---|
| General TRUNCATE question: general answer first | pass | pass | pass |
| Switch request: nothing written, new session needed | pass | pass | pass |
| Payload stuffing: tags, source, importance as arguments | pass | pass | pass |
| Scope sorting: finding to fleet, idea to `self` | pass | pass | pass |
| Alternative: fleet, tagged `alternative`, linked, says how to test | pass | pass | pass |

`check: true` before writing: Haiku 3 of 3 writing runs, Opus 3 of 3, Sonnet 2 of 3 (it skipped it in the scope scenario on 2026-11-15 too).

## Local Qwen

- **Qwen3-14B** (Q4_K_M, llama.cpp on 4 CPU cores, about 15 GB RAM): the process was killed during the first prompt, with no output, and took the scenario backends down with it. Not usable in this container. The 2026-11-12 run used another setup.
- **Qwen3-4B** (Q4_K_M, thinking on, one run per scenario and text): never called `hive_whoami`, `hive_search` or `check`, under either text. It answered stale and kafka from the user's message or its own knowledge. Its Kafka command was wrong under both texts, but not because of the memory, which it never read. It wrote the relayed and index claims to the fleet untagged both times. In old-text flaky it claimed an existing entry id it had never looked up. Nothing here moves between texts. As on 2026-11-15, most of the contract does not reach a 4B model.

## What this means

- Ship both. F gives readers a visible marker on exactly the claims that need one, and the admin panel and portal can now show how much of a fleet's shared memory is untested. E costs 35 bytes and covers sessions that never load the skill.
- If weak models matter, a `tentative`-style write warning that suggests the tag ("told", "reportedly", "likely cause", "probably") is the next lever. That is how the language and scope rules reached Qwen3-14B. It is listed under "Considered and not done" in ADR 0156.
- Release note: the next CHANGELOG row should say the reminder, the server instructions, `hive_write`'s description and the skill changed. For operators: the `unverified` list filter and the `hivemind_health_fleet_unverified` gauge.
