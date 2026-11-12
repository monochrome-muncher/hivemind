# Scope by evidence: fleet for what held, self for ideas

Amends the write guidance of [ADR 0085](0085-the-mcp-package-splits-into-contract-server-and-runners.md) (the agent contract text lives in `contract.py`) and the explore hint of [ADR 0143](0143-fleet-phases.md). Builds on [ADR 0011](0011-fleets-and-trust-levels.md) (scopes) and [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) (a fleet entry may supersede its writer's own `self` entry). Changes no code path, permission or ranking.

Status: Accepted.

## Context

A user brainstormed with their agent, and the agent wrote half-formed ideas to the fleet, where the user feared they would mislead other agents. The agent text sorted scope by audience, not by evidence:

- The skill said "Omit `scope`" (fleet) and "`self` only for what concerns only you".
- The server instructions said "omit scope so they reach your fleet".
- "Never write ... unverified guesses (mark a lead "unverified")" read as permission to write a guess if it was marked.
- "An untested plausible alternative gets its own entry" and the explore-phase hint ("write untested approaches") invited untested ideas into the fleet.

Nothing named brainstorms, plans or work in progress as `self` material.

## Decision

1. **Scope by evidence.** The skill's scope rule becomes: omit `scope` (fleet) for what the agent saw hold or its user decided; `self` for ideas, brainstorms, untried plans, the user's preferences and work in progress. Once one is tested, the agent supersedes the self note with a fleet entry, which ADR 0033 already allows.
2. **The server instructions say it too**, for harnesses that never load the skill: "omit scope so tested findings reach your fleet, and keep untested ideas in self."
3. **"Never write" names untested ideas to the fleet** instead of "unverified guesses (mark a lead unverified)".
4. **An `alternative` entry is one someone could test**, and says how. The explore-phase hint says the same.
5. **Fleet stays the default.** The rule adds a reason to choose `self`; it does not ask agents to hold back findings. Writing the same note to both scopes is not encouraged: the copies would trip `near_duplicate` and drift apart. The two shapes of "both" are a `self` note that links (`see_also`) to a fleet entry, and a `self` draft promoted with `supersedes`.

To fit the 18,000-byte always-on budget the skill also loses its one-line pinning paragraph (`hive_pin`'s description already says "Pin rarely"), and two sentences are shortened. Measured total: 17,950 bytes.

## Consequences

Checked live on 2026-11-12 (write-up `docs/reviews/2026-11-12-memories-as-data-and-scope.md`):

- Claude Opus, Sonnet and Haiku, each given the full contract text and one of five scripted sessions (two brainstorms, a verified fix, a fix plus a passing idea and a personal preference, a team decision), old text against new, one run each: no model wrote a brainstorm to the fleet under either text. Every verified fix and the team decision went to the fleet under both (18 of 18). With the new text, brainstorms went to `self` in 4 of 9 runs instead of being dropped.
- Qwen3-14B in OpenCode (the skill never loaded; it saw the reminder, server instructions and tool descriptions) wrote no brainstorm entry under either text, but folded a user's passing "maybe Celery someday" into a shared `decision` entry about a verified fix, under the old text and the new one alike. Like the English rule (ADR 0134), the scope rule holds on frontier models from text alone and does not hold on the 14B.
- The rule is text only, so a weaker model may still ignore it. A write warning on brainstorm wording is the next step if production shows that.
