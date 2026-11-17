# `hive_whoami`'s description says a switch is a new session

Amends [ADR 0150](0150-key-profiles-on-the-client.md) decision 6 ("it costs no always-on text"). Backs decision 3 (a switch is a new session) with one clause in a tool description, the text every harness shows.

Status: Accepted; decision 1 amended by [ADR 0158](0158-agent-text-drops-weak-model-clauses.md): the description keeps the fact, not the how-to.

## Context

On 2026-11-13 Qwen3-8B in Pi was asked to switch Hivemind to the user's client profile. It never loaded the `hivemind-switch` skill (listed with its trigger words) and never loaded the `hivemind` skill either. It tried `hive_pin` on an invented id and `hive_register` with the agent key, then wrote a shared `decision` "Switch to client profile", pinned it in the team fleet's briefing, and told the user Hivemind had been switched. Nothing was switched, and the team fleet got a false entry at the top of its briefing (the live check of the 2.25 and 2.26 agent text on 2026-11-13, summarised in [the 2026-11-15 plugins and skills review](../reviews/2026-11-15-plugins-skills-review.md)).

Weak models do not load on-demand skills, so text that lives only there does not reach them. Pi does not show the server `instructions` either. The tool descriptions reach every harness, and `hive_whoami` is the tool the model called first.

## Decision

1. `hive_whoami`'s description ends: "The key is fixed per session: to act as another agent or fleet, ask the user to start a new session (hivemind-switch skill)." It is an instruction, not only a fact, because telling the user is the step the model skipped, and it says "act as" so that a privileged agent does not read it as "reading another fleet needs a new session".
2. The bytes come out of other always-on text that says nothing new: `hive_get`'s "an entry you may not read answers not_found" (the same description's `entry_ids` clause, `hive_withdraw` and `hive_feedback` say it), `hive_search`'s "'supersedes': entries it replaced", `hive_write`'s "hive_get shows the links on both ends" (`hive_get`'s description says it) and the skill intro's "rather than local memory files" (the skill's description and §5 say it).

## Consequences

- The clause reaches every harness that lists the tools, with or without the skills, and a harness that loads tools lazily sees it when it loads `hive_whoami`.
- Whether a weak model acts on it is checked live, not assumed: [the 2026-11-15 plugins and skills review](../reviews/2026-11-15-plugins-skills-review.md) has the run.
- The always-on total stays under the 18,000-byte budget.
