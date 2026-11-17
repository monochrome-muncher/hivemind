# The skill does not repeat what a tool description or the server's reply says

Amends [ADR 0081](0081-upstream-changed-on-hits.md) decision 8 and the skill line of [ADR 0087](0087-near-duplicate-write-warning.md). Builds on [ADR 0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) (one home for the agent contract text) and [ADR 0077](0077-write-check-mode-and-warnings.md) (warnings on a write).

Status: Accepted.

## Context

The always-on text (the `hivemind` skill, the server `instructions`, the ten tool descriptions and the reminder) shares one 18,000-byte budget. After [ADR 0153](0153-general-questions-get-general-answers.md) it measured 17,956 bytes, so every new rule had to find its bytes in a hurry.

Reading the text against the server showed two kinds of repetition. Some rules stood in both the skill and a tool description. A session that has the skill has the descriptions too, while several harnesses and most weak models never load the skill, so the description is the copy that counts. Other sentences described a reply that explains itself when it arrives: the registration messages, the `supersede_denied` message, the `local_path` and `near_duplicate` warnings.

## Decision

1. A rule that a tool description states is not repeated in the skill. Cut from the skill: the `upstream_changed` re-check line (the `hive_search` description has it), the exceptions to the English rule and the list of kinds (the `hive_write` description has both), and the `hive_whoami` table row for a fleet writer, which asked for nothing. The server `instructions` drop "the shared org key" from the `key_kind` sentence; `hive_whoami` and the setup skill say what the org key is.
2. A sentence that only describes a reply is cut when the reply says the same at the moment it applies. Cut: the reply walk-through in `hive_register`'s description, "an explicit out-of-permission scope is rejected" (the `permission_denied` reply names the trust level and the scope), the skill's pointer to the `near_duplicate` warning, and its how-to for local paths (the rule itself stays). The skill keeps what to do on `supersede_denied`; the reply says that only a chain's current head can be superseded.
3. A clause about what the caller may not read is cut where the agent has nothing to act on: the "entries you may not read answer `not_found`" notes on `hive_withdraw` and `hive_feedback`, and `hive_get`'s "limited to what you may read" on the history and the links. No reply says these. An unreadable entry and an unknown id get the same answer on purpose ([ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md)), and `hive_get`'s `entry_ids` clause still says `not_found` covers ids the caller may not read. This also removes the copies that [ADR 0151](0151-whoami-says-a-switch-is-a-new-session.md) decision 2 pointed to.
4. Advice that belongs to one error moves into that error. `embedding_unavailable` over MCP now says "don't retry in a loop: keep the entry locally and write it later", and `hive_write`'s description no longer does. The REST message is unchanged.
5. Tests pin the replies and the description clauses the trimmed text relies on, so a later edit of a message cannot silently drop the rule.
6. The server `instructions` still repeat the skill's core. They are the only rules some harnesses keep after compaction, and the only ones a session without the skill sees.

The total measures 17,107 bytes. The budget stays at 18,000.

## Considered and not done

- **Lower the budget to the new total.** The point of the trim is room for the next rules.
- **Move the foreign-fleet rule out of `hive_search`'s description** into a note on replies that hold a foreign hit (about 330 bytes). It needs a new reply field; left for when the room is needed.
- **Reword the rest for length.** Each reworded rule is a small gamble on behaviour for a few bytes.

## Consequences

- 849 bytes are free for later rules.
- An agent that skips `check: true` learns the local-path how-to only from the warning on the stored write.
- Checked before and after on the known failure cases; see [the 2026-11-15 always-on trim](../reviews/2026-11-15-always-on-trim.md).
