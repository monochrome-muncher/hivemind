# Finding, learning from and testing what the hive knows: new ideas

> A research proposal, kept for reference. It is not the source of truth: [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions. Status notes are as of the date given.

*Proposal, 2026-10-20. Checked against `main` at a0cebb7 (2.10.0 plus the fleet pin permissions change).*

### Status (updated 2026-10-29)

| Idea | State |
|---|---|
| §1 query instruction | Shipped in 2.11.0 (ADR 0097; on by default, pooled MRR 0.835 → 0.881) |
| §2 "what changed under you" filter | Shipped in 2.11.0 (ADR 0098, `relied_on_changed`) |
| §3 answers-an-open-question warning | Shipped in 2.11.0 (ADR 0099) |
| §4-§9 | Kept as candidates, not approved |

## The question

The question was whether anything from hive-mind or collective-intelligence research, or from Caura, is still missing from Hivemind and would help agents (1) find memories, (2) learn from each other, and (3) explore and test the hive's knowledge when it is relevant.

## The short answer

The 2026-10-10 study and its follow-ups already took most of the good ideas: evidence on hits, `last_confirmed_at`, `contested`, `upstream_changed`, `explore`, pins, the fleet map pin, write checks, `near_duplicate`, readable and batch feedback, and narrow importance. What is still missing falls into three gaps:

- **Corrections don't reach the agents who relied on the old answer.** When an entry is superseded or flagged, Hivemind shows that on the entry. It never tells the agents who rated it `helpful` and are still working from it. The research term for this is "stale propagation" or "retractions don't travel".
- **Search is still leaving easy findability on the table.** Queries are embedded without the instruction that Qwen3 was trained to expect, and tags are neither embedded nor keyword-indexed.
- **No one's job is to tend the hive.** Re-checking old knowledge, answering open questions and distilling fragments happen only when a busy agent happens to stumble on them. The fix that research and Caura point to is a dedicated agent that does this on a schedule ("sleep-time" consolidation). Hivemind can add it as an on-demand skill, with no server LLM.

**I recommend these four first:**

| Rank | Idea | Why first | Effort | Always-on bytes |
|---|---|---|---|---|
| 1 | **Qwen3 query instruction** (§1) | Every search gets better, and agents change nothing. The model card reports a 1-5% retrieval loss without it. | S, plus a 70/20/10 measurement | 0 |
| 2 | **"What changed under you"** (§2) | Corrections reach the agents who relied on the old entry. This is the missing half of the feedback loop. | S-M | about +140, paid for by §5 or a trim |
| 3 | **Answers-an-open-question warning** (§3) | When a write answers an `open-question` or `anomaly` entry, the writer is told to supersede it. | S | 0 |
| 4 | **Re-check queue and a librarian skill** (§4) | Testing old knowledge becomes someone's job. It's on-demand, so it costs no always-on bytes. | S each | 0 |

The rest are in §5-§9, in order of expected value. §10 lists what I considered and left out, including the Caura features that collide with recorded decisions.

Every idea below is application-side, needs no new infrastructure, and is one PR. Nothing adds a ranking term that reads tags, authors, links or counts, so the guardrail invariants (ADR 0070) are unaffected. None re-proposes a dropped item: no `question` kind, no hypothesis incubation, no Elo, no recall-count boost, no keystones, no auto-supersession or LLM merging, and no LLM enrichment on write.

---

## 1. Embed queries with Qwen3's retrieval instruction

**Gap.** `SearchService._vector_stream` embeds the raw query with the same call used for entries (`services/search.py:407`, `embed_text`, which is `embeddings/openai_compat.py:141`). Qwen3-Embedding is instruction-aware. Its model cards say queries should carry a one-line task instruction (`Instruct: <task>\nQuery: <text>`) and documents should not. They also say that leaving the query instruction out "can lead to a drop in retrieval performance by approximately 1% to 5%". The 2026-10-10 study listed this under "Not now" and asked for a separate retrieval experiment. That experiment has not been run.

**Change.** Add an `embed_query` port method that prepends a configurable instruction, for example `HIVEMIND_QUERY_INSTRUCTION="Given an agent's task or question, retrieve shared memory entries that answer it"`. It is empty and off by default, and the provider wraps the instruction only for queries. Entry embeddings, `related`, `near_duplicate` and version grouping compare entry vectors with entry vectors, so they are unchanged and nothing needs re-embedding.

**Measure before turning it on.** Run the eval harness with Qwen3 8B, 4B and 0.6B weighted 70/20/10, per domain and pooled, comparing no instruction, one generic instruction and one per-kind instruction. The instruction shifts every query vector, so in the same run re-sweep the optional `HIVEMIND_VECTOR_MIN_SIMILARITY` (0.45 is recommended today) and check that the cosine > 0 cut-off (ADR 0061) still behaves. Keep it as a `make measure-*` target, outside pytest.

**Risk.** The setting has to match the deployment's embedder. A non-Qwen embedder may be hurt by the instruction, which is why it ships off. It needs an ADR, because it amends SPEC §7 (what is embedded).

**Research.** Qwen3-Embedding model cards (0.6B, 4B and 8B). HyDE (Gao et al. 2023) and "encoding specificity" (Tulving and Thomson 1973) both point the same way: the cue has to match how the memory was encoded.

## 2. "What changed under you": deliver corrections to those who relied on an entry

**Gap.** When an entry an agent confirmed is later superseded, withdrawn or flagged `stale` or `wrong`, nothing tells that agent. Today:

- `upstream_changed` covers only insight and decision entries that link to the changed entry with `see_also`.
- `changed_since_pin` covers only pins.
- The catch-up step lists only an agent's *own* flagged entries (`skills/hivemind/SKILL.md:43`).

An agent that rated something `helpful` last week, and may have built work or local notes on it, never hears that it was retracted. This is the classic failure in science, where retractions don't travel (Budd et al. 1998). It is also one of the four shared-memory failures named in "Governed Shared Memory for Multi-Agent LLM Systems" (arXiv 2606.24535), "stale propagation". Truth maintenance (Doyle 1979) gives the remedy: whoever holds a belief should re-examine it when its premise changes.

**Change.** Add a `hive_list` filter, `relied_on_changed: true`. It returns entries the caller (its agent name) rated `helpful` that have since been superseded, withdrawn, or reported `stale` or `wrong` after that rating, each with its current head. The filter has two parts:

- One `EXISTS` clause in `_filter_conditions` (`store/pgstore.py:801-856`, next to the `flagged` clause at `:848`), comparing `feedbacks.updated_at` with the newest flag or the successor's `created_at`. The agent's own rows already sit in `feedbacks` (`schema.sql:120-128`). If a deployment grows large, an index on `feedbacks(agent)` would keep the probe cheap (a small migration).
- It forces `include_inactive` for this filter only, so the superseded entry itself is listed alongside its current version.

**Skill.** Catch-up step 3 becomes "`hive_list` with `author` = your `name` and `flagged: true` (fix your own), then `relied_on_changed: true` (re-check what you built on)". That is about 70 bytes, plus about 70 for a clause in `DESC_LIST` (`mcp/contract.py:95`). The budget stands at 17,987 of 18,000 bytes, so this needs about 140 bytes found elsewhere. §5 frees about 68 bytes by deleting "tags filter, they are not searched" (`SKILL.md:107`). The rest can come from merging the two catch-up `hive_list` lines into one sentence. If §5 is not built, the trim must come from elsewhere. Candidates are the `similar` clause in `DESC_SEARCH`, which describes an off-by-default feature, and the explore line.

**Why it matters for collective learning.** Feedback today flows from readers to the entry. This sends the correction back to the readers. It is the second half of the loop and needs no new signal.

**Effort.** S-M. No ADR is strictly needed (it is a filter, display only). Add one contract test: an agent rates A `helpful`, A is superseded, and the filter returns A with its head. Add a second: B is flagged after the rating and is returned.

## 3. Tell a writer when their entry answers an open question

**Gap.** The skill asks agents to tag a gap `open-question` and says "the answer supersedes it" (`SKILL.md:125-127`). The same goes for `anomaly` facts. But the agent who later learns the answer usually doesn't know the question exists. Nothing connects them, so open questions pile up unanswered.

**Change.** On write and `check: true`, the write path already computes the nearest readable entries (`services/write.py:97-100`, `_related_best_effort`). If one of them is active and tagged `open-question` or `anomaly`, add a warning `answers_open_question: <id>`. Its text says: "this may answer an open question: if it does, write with `supersedes` (you may) or `see_also`". It sits next to `near_duplicate_warning` in `services/write_warnings.py:66`. Warnings travel in the response, so it costs no always-on bytes.

**Optional second step.** A `tags_any` option on `hive_list` would let the catch-up step say "open questions in your systems' tags". I'd wait for data, since the user dropped the `question` kind for being potentially noisy. This keeps to the existing tag and adds no kind.

**Research.** Stigmergy (Grassé 1959; Theraulaz and Bonabeau 1999): a trace coordinates only if each state has a known response. Here the response is "answer and supersede". The blackboard architecture's posted goals (Hearsay-II), without a new kind.

**Effort.** S. Add a unit test in the write-warnings suite.

## 4. A re-check queue, and a librarian skill that works it

### 4a. Re-check queue

**Gap.** Popper's point, which the study adopted, is that standing is earned by tests and dated by the last one (`last_confirmed_at`). Nothing helps an agent *find* what most needs a test. Old, important, never-confirmed entries drift into what Assmann calls the "floating gap": nobody re-performs them, so nobody knows if they still hold.

**Change.** Add a `hive_list` filter, `unconfirmed_days: N`. It lists active entries whose `last_confirmed_at` is empty or older than N days, and that are themselves at least N days old. Results are ordered by importance, then age. It reuses the confirmation logic in `feedback_signals` (`store/pgstore.py:1556`, `last_confirmed` at `:1595`) as a SQL clause in `_filter_conditions`. It is a filter only, never ranking, so invariant I2 holds. That matters because "lift unrated entries" was dropped from explore for exactly this reason (I2).

### 4b. `hivemind-librarian` skill (on demand)

**Gap.** Distilling, conflict settling and flag fixing all depend on a working agent having spare attention. Caura answers this with server-side LLM residents: `caura_insights` focus modes, the Forge skill factory and the opt-in Interviewer. Hivemind rejected those on the server. The research answer is "sleep-time compute" (Lin et al. 2025) and the after-hours librarian: consolidation by a dedicated agent, off the critical path.

**Change.** Add a third skill, loaded only when invoked, for an agent the operator schedules (a cron job, a routine, or an idle agent). Its loop:

1. Work `flagged: true`, then `relied_on_changed` (§2), then `unconfirmed_days: 90` (§4a): test what it can and rate with `verified:` notes.
2. Look for `open-question` and `anomaly` entries it can now answer (§3).
3. Distill clusters where three or more active entries answer one question, under the existing rule (`SKILL.md:119`).
4. Settle `Conflict:` facts it can test.
5. Refresh the fleet-map pin's `see_also` links and canonical tags, using §6 if built.

The librarian is an ordinary contributor key with no new permissions. It writes, rates and supersedes like anyone else, so attribution and the audit trail stay intact. It is also rate-limited by its own guidance: no more than a small, fixed number of supersessions per run, and never withdrawing others' entries.

**Cost.** None in the always-on budget, because the always-on test counts only the main skill, the server instructions, tool descriptions and the reminder (`tests/unit/test_agent_plugin.py:315-330`). Harnesses do list each skill's one-line description, about 200 bytes.

**Decide first.** SPEC §10 holds a server "Curation workflow" behind the trigger "an org wants a librarian" (`SPEC.md:351`). This skill is client-side practice, not server roles, so I don't think it fires that trigger. But the user should decide whether it does.

**Effort.** S each: 4a is a filter plus tests, and 4b is a skill, docs and a size test.

## 5. Make tags findable (closing ROADMAP §4.2 (a))

**Gap.** Tags are filters only. The keyword index covers summary (weight A) and body (weight B) (`store/schema.sql:33-40`), and the embedding covers summary plus a body prefix (`domain/entry.py:400-414`). An entry tagged `pgbouncer` whose text says "the pooler" cannot be found by searching "pgbouncer". The skill spends 68 bytes warning agents about this (`SKILL.md:107`). ROADMAP §4.2 (a) (`ROADMAP.md:264-269`) lists embedding tags and entity names as open and unmeasured.

**Change, in two separable steps:**

- **Keyword step, low risk.** Add `tags` to `search_tsv` at weight C. This is one migration that replaces the trigger function and backfills with an `UPDATE`. It needs no re-embedding and leaves the vector stream unchanged. The guardrail is unaffected, because keyword rank is a retrieval stream, not a ranking term.
- **Embedding step, needs measurement.** Append `Tags: a, b` (and optionally entity names) to `embeddable_text`. Measure at 70/20/10 on the five domains, together with §1, since both change vectors. If it helps, operators re-embed. A re-embed is already a supported operation; check the runbook before claiming it is cheap.

**Payoff.** Better recall for tag-heavy fleets, and the skill sentence can go, which pays for §2's skill line.

**Effort.** M, with a migration and an ADR (amends SPEC §6.1 and §7).

## 6. A table of contents for the hive

**Gap.** Transactive memory (Wegner 1987) says a group remembers through a directory of *who knows what*. Hivemind's directory today is the fleet map pin, which is hand-kept, and search, which only answers what you think to ask. A new agent cannot see what the hive covers. Tag fragmentation (`postgres` / `pg` / `postgresql`) also goes unnoticed, because nobody sees the tag list.

**Change.** Add a `hive_list` option, `facets: true`. Instead of items, it returns the caller's readable active entries grouped by tag, up to 50 tags. Each tag shows its active count, newest `occurred_at`, flagged count and `open-question` count. It deliberately leaves out authors (the Matthew effect; the study rejected author facets on search) and helpful counts (a popularity cue). It is one `GROUP BY unnest(tags)` under the same visibility clause (`store/pgstore.py:1348`).

**Use.** The catch-up step is unchanged. Agents and the librarian use it to pick canonical tags before writing (the skill already says "reuse tags"), and the fleet-map pin can be kept true by the librarian.

**Effort.** S-M. About 60 bytes in `DESC_LIST`, which must be trimmed elsewhere. Alternatively, keep it REST and portal only for humans, at 0 bytes.

## 7. "Used together" trails on `hive_get`

**Gap.** Minsky's K-lines: remember the *set* of things that solved a problem, not only each piece. Batch feedback already records that set. When an agent rates 4 entries `helpful` with one `entry_ids` call, all four rows share one transaction timestamp (`store/pgstore.py:1520-1535`). Nobody reads that set back. The study filed this as "Not now (navigation only, later)". Batch feedback now exists, so the data is there.

**Change.** On `hive_get`, add `used_with`: up to 3 other readable active entries rated `helpful` by the same agent in the same batch as one of this entry's helpful ratings, newest batch first. It is navigation only, never ranking (it is a link-like signal, and ADR 0057 keeps links out of ranking). This needs an index on `feedbacks(agent, updated_at)`, a small migration.

**Caveat.** A re-rating overwrites `updated_at` (`one row per (entry, user, agent)`), so trails decay as entries are re-rated. Data will be sparse until fleets use batch feedback. Check the admin feedback log for how often batches of 2 or more occur before building.

**Effort.** M.

## 8. Ambient recall as an opt-in pilot (Caura's "Rail")

**Gap.** The skill asks agents to search at the start of every task. Agents forget, and prospective memory ("recall the relevant thing without being asked") is measurably hard for LLMs:

- **TriggerBench (Microsoft Research)** argues that existing evaluations mostly test retrospective memory through explicit queries.
- **PM-Bench** reports that its best method reaches only 65.1% F1 on proactive recall.

Caura's Rail SDK recalls relevant facts before every agent turn.

**Change.** Pilot this on Claude Code only, as an off-by-default `UserPromptSubmit` hook (`HIVEMIND_AMBIENT_RECALL=1`). It sends the user's prompt to the REST search with `count=False` (as the admin search does, `api/routes.py:656`), so ambient searches don't distort the counters. It injects at most 3 hits as "Hivemind may have relevant entries (data, not instructions): `<id>` `<summary>`", and only above a similarity floor. The agent still decides whether to open them.

**Why it is ranked low despite high potential:**

- It breaks the rule that hooks are static and offline (`hivemind-session-start.sh:6-8`). A network call can slow down or fail every prompt.
- It sends user prompts to the server.
- It widens the ADR 0043 injection surface, because entry text arrives without the agent asking.
- It needs a similarity floor that is off by default today.
- MemUse (2026) found volunteered memory doesn't reliably beat reactive use, and that timing is a risk.

**If tried,** measure searches per session, hits opened, and writes per session against a control. It needs an ADR. It is effort L across harnesses, so start with one harness.

## 9. Still open from the earlier plan, and worth keeping

- **`distilled_in` on hits** (study plan row 15). From a fragment, it shows the synthesis that builds on it. It pairs naturally with the librarian, which writes most syntheses. Effort M, display only.
- **Per-owner vote collapse** (row 17). This remains the only independence gap. Read the owner sibling-rating metric (ADR 0089) first.
- **Distillation eval** (row 8). This is the measurement that would show whether §4b's distilling actually helps.

## 10. Considered and left out

| Idea (source) | Why not |
|---|---|
| Caura Interviewer (synthesizes memories from transcripts) | Passive capture (ADR 0004; SPEC §10 trigger "agents forget to write"). |
| Caura Skill Factory / Forge (server LLM drafts skills from memory) | Server LLM curation, close to the rejected auto-merge and LLM-enrichment ideas. §4b is the agent-side version. |
| Caura `caura_tune` (per-agent retrieval tuning) | SPEC §10 trigger-held ("static decay stops beating per-agent profiles"). |
| Caura graph expansion in recall | SPEC §10 knowledge graph, trigger-held. Entity facets (ADR 0016) are the pre-staged half. |
| Caura activity digests | They are LLM-written. A plain "what's new" already exists in the 7-day catch-up, and admin stats cover humans. |
| A `procedure` or `playbook` kind (Voyager; Agent Workflow Memory; Multi-Agent Transactive Memory, arXiv 2606.19911; Caura skills collection) | Still data-gated, like the parked kinds. First count how many `insight` entries are really how-tos (one admin query). Don't add a kind. |
| "Hot topics this week" in catch-up | Visible activity counts invite herding (Salganik et al. 2006). §6 shows coverage without a ranking or "trending" cue. |
| Logging failed search queries to find gaps | ADR 0056 counts empty searches deliberately without query text. §3 gets at gaps through agent-written `open-question` entries instead. |
| Rewarding exploration (points or badges for answering questions) | Goodhart. The study kept agent-facing metrics out of the briefing for the same reason. |

## Sources

- Qwen3-Embedding model cards: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B, https://huggingface.co/Qwen/Qwen3-Embedding-4B, https://huggingface.co/Qwen/Qwen3-Embedding-8B-GGUF
- Caura README (features: Rail SDK, `caura_insights`, Skill Factory, Interviewer, `caura_tune`, digests): https://github.com/caura-ai/caura (read 2026-10-20)
- Governed Shared Memory for Multi-Agent LLM Systems, arXiv 2606.24535 (via https://www.emergentmind.com/papers/2606.24535)
- Multi-Agent Transactive Memory, arXiv 2606.19911 (via https://papers.cool/arxiv/2606.19911). Abstract-level only; the full text was not reachable from here.
- TriggerBench: https://www.microsoft.com/en-us/research/publication/triggerbench-investigating-prospective-memory-for-large-language-models/
- MemUse, arXiv 2608.24189: https://arxiv.org/pdf/2608.24189 (abstract-level)
- Du, Memory for Autonomous LLM Agents (survey), arXiv 2603.07670: https://huggingface.co/papers/2603.07670
- Earlier sources (Popper, Assmann, Wegner, Minsky, Doyle, Budd et al., Lin et al. sleep-time compute, Grassé, Salganik et al.) are as cited in docs/proposals/2026-10-10-hive-mind-ideas.md, Appendix A.

Numbers from search snippets (Qwen's 1-5% and PM-Bench's 65.1% F1) were not checked against full texts.
