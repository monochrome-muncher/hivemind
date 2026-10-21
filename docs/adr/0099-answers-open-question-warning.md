# A write warns when it may answer an open question

Amends [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes) and builds on [ADR 0052](0052-writes-report-the-nearest-entries.md) and [ADR 0087](0087-near-duplicate-write-warning.md). Adds a row to SPEC §4.1. Adds no entry kind and no tag. Does not refuse, merge or supersede anything (ADR 0001).

Status: Accepted. Always on, advisory only.

## Context

The skill asks an agent to tag a gap `open-question` and an unexplained surprise `anomaly`, and says the answer supersedes it. The agent that later learns the answer rarely knows the question exists, so open questions pile up with no reply.

This is a stigmergy problem. A trace coordinates agents only if each state it can show has a known response (Grasse 1959; Theraulaz and Bonabeau 1999). An open question is a state, and until now nothing made the response, writing the answer to it, a known next step for the agent holding the answer. The write already finds the nearest entries (ADR 0052), so the signal costs no extra round trip.

The user dropped a separate `question` kind, and this does not bring it back. It reads the two tags the skill already names.

## Decision

1. **`answers_open_question`** joins the advisory codes. It fires on writes and checks when an entry in `related` is active, carries the tag `open-question` or `anomaly`, has a cosine `similarity` of at least **0.70**, and is not named in the draft's `supersedes` or `see_also`.
2. **One warning per write**, naming the closest such entry and, when others qualify, how many more. The message says: may answer open question `<id>` (similarity 0.xx): if it does, write with `supersedes` (if you may) or `see_also` to it.
3. **Two readings, no fixed remedy.** Superseding closes the question, as the skill says, but the writer may not be allowed to supersede it (`may_supersede`, ADR 0033), or the entry may only partly answer it. `see_also` links it without closing it.
4. **A write tagged `open-question` or `anomaly` gets no warning.** Two questions near each other are not an answer.
5. **Best-effort.** The warning reads `related`. If the lookup failed, `related` is empty and there is no warning. The lookup already leaves out the new entry and, in check mode, the entries the draft supersedes; the warning also skips them by id.
6. **0.70 is provisional.** It sits below the near-duplicate floor (0.90, ADR 0087) because an answer shares a subject with its question without repeating it. It is a constant, `_OPEN_QUESTION_SIMILARITY`, to be checked against Qwen3-Embedding-8B measurements. Another embedder places related text in a different band and should be re-checked with the same pairs.

## Consequences

- An agent that learns an answer sees the open question in the write's reply, where it can link or supersede it, with no new tool and no always-on text. The warning explains itself.
- A false warning costs a second look and nothing else. A missed one leaves things as they are today.
- Only a `related` entry (three at most, ADR 0052) can trigger it, so a question that is not among the three nearest is missed.
- Questions tagged some other way (`question`, `todo`) are not seen. The two tags are the convention.
