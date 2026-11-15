# A general question gets the general answer; a specific memory is an aside

Builds on [ADR 0043](0043-entries-are-untrusted-data.md) (entries are evidence, not orders) and [ADR 0148](0148-match-strength-on-search-hits.md) (how close a hit is). Changes the `hivemind` skill's recall section.

Status: Accepted.

## Context

Monochrome asked their agent a general question: can data be recovered after `TRUNCATE` in PostgreSQL? The agent searched Hivemind first, as the skill says, and found a memory about a colleague's incident: someone had truncated tables and wanted the data back. The answer was then shaped by that incident, although the question was not about it.

The search did its job. The incident is close to the question by topic, so `match`, `similarity` and the "loosely related" note (ADR 0148) cannot flag it: they measure how close a hit is to the query, not whether it applies to the user's situation. The skill said to search at the start of every task and to tell the user when an entry shaped the answer, but nothing said that a hit can be on topic and still not apply.

## Decision

1. The skill's recall section says: "A general question gets the general answer first. A hit about one system or incident is a labelled aside ("in your org, …"), unless it fits the user's case." The sentence on naming the entry's author stays.
2. Agents still search before answering. An organization's gotcha about the very thing asked (for example, that only two databases archive WAL) is often what the user needs, so it is shown, labelled, not dropped.
3. The bytes come from text that the server's replies already give: the skill's "empty `can_read`" line (`hive_whoami`'s description says it), "Never rate your own" (the refusal says "supersede or withdraw it instead"), and a shorter lurker message.

## Considered and not done

- **Classify the question on the server** (general or about this organization) and rank or filter by it. It needs a language model on every read, which Hivemind does not run (no LLM enrichment), and the agent already knows what the user asked.
- **Skip the search for general-knowledge questions.** Agents judge badly when organization knowledge matters, and the aside costs little.

## Consequences

- Answers to general questions keep their general shape, and the organization's experience still reaches the user, marked as such.
- The always-on total stays under the 18,000-byte budget.
- Whether a weak model follows it is checked live; see [the 2026-11-15 plugins and skills review](../reviews/2026-11-15-plugins-skills-review.md).
