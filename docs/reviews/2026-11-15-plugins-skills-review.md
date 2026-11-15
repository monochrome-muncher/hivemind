# Plugins and skills review, 2026-11-15

Baseline: main at e2bea82 (2.26.0 plus #433 to #453). Scope: `plugins/hivemind/` and `plugins/antigravity/`, the four skills (`hivemind`, `hivemind-setup`, `hivemind-update`, `hivemind-switch`), their references, the plugin README (read in full), DEVELOPING.md and CHANGELOG.md, checked against the server code (`mcp/contract.py`, `mcp/app.py`, `services/`).

## Result

The docs and skills match the code closely. Every tool name, argument, reply key and warning the skills mention exists and behaves as described: `check: true`, the `fix` hint on `stale` and `wrong` feedback, `relied_on_changed`, `not_english`, `tentative`, the loose-match note, `ignore_age`, incognito on all fourteen launcher names, and key profiles through `hivemind-as`. Every path, link and drift-check option quoted in the plugin docs resolves after the moves in #442 to #453.

The failure modes worth fixing were about how agents use the text, not drift:

| # | Finding | Fix |
|---|---|---|
| 1 | A weak model asked to switch to the user's client profile did not load `hivemind-switch` (or `hivemind`). It wrote a shared "Switch to client profile" decision, pinned it, and said it had switched (Qwen3-8B in Pi, 2026-11-13). | `hive_whoami`'s description now says the key is fixed per session and a switch is a new session the user starts ([ADR 0151](../adr/0151-whoami-says-a-switch-is-a-new-session.md)). Tool descriptions reach every harness. |
| 2 | In the same run the model put `scope`, `importance`, `sources` and `tags` inside `payload`. The entry was stored with defaults and the reply said nothing. | New advisory warning `payload_fields` ([ADR 0152](../adr/0152-payload-fields-write-warning.md), #455). |
| 3 | The skill's "Never write ... untested ideas to the fleet" contradicted §4, which asks for a fleet entry tagged `alternative` for an approach someone could test (ADRs 0146, 0147). | The line now excepts §4's `alternative`. |
| 4 | Reported by Monochrome: a general question ("can data be recovered after TRUNCATE in PostgreSQL?") was answered through a colleague's incident memory. The hit was close by topic, so no relevance signal could flag it. | The skill says a general question gets the general answer first, and a hit about one system or incident is a labelled aside unless it fits the user's case ([ADR 0153](../adr/0153-general-questions-get-general-answers.md)). |
| 5 | Small drift: Copilot CLI and Junie versions checked today were not named; Antigravity 1.5.8+ refuses 2.26.0 and no newer release exists yet; the setup skill's incognito list missed `agent`, `code` and `codium`; `hivemind-switch`'s Junie row read as if the agent should run a command that reads the key file. | #454 (merged). |

Items 1, 3 and 4 share the 18,000-byte always-on budget. They are paid for by dropping sentences that other always-on text, or the server's own replies, already say. The measured total is 17,956 bytes.

## Live checks

**Setup.** Pi 1.5.2 from npm, the plugin installed from the working tree, `hivemind-mcp-http` on a scratch database with a hash embedder, and a local model through llama-cpp-python on CPU. Qwen3-8B ran out of memory in this container, so the Pi runs used Qwen3-4B (Q4_K_M). A small proxy renders Qwen's own chat template, because llama-cpp-python's tool-calling format drops tool descriptions, which would have hidden finding 1's fix. Building llama.cpp from source was not allowed here. Rig: project files `reviews/2026-11-15-plugins-skills-rig/`.

**Switch request in Pi (old and new text, one run each).** In both runs Qwen3-4B called a tool named `hivemind-switch` (the skill's name), got "not found", and told the user it could not switch. It wrote and pinned nothing, but it never called `hive_whoami` either, so the new clause was not exercised. Inconclusive for finding 1. The fix rests on the 2026-11-13 transcript, where the model's first call was `hive_whoami`.

**General TRUNCATE question in Pi.** Qwen3-4B answered from general knowledge without searching Hivemind, so the rule was not exercised there.

**General TRUNCATE question, simulated with Claude models.** Each agent got the skill (old or new), the user's question, and the real search and `hive_get` results from the scratch pool (the incident and a related decision). Each wrote the reply it would send:

| Skill | Haiku, run 1 | Haiku, run 2 | Sonnet |
|---|---|---|---|
| Old | The incident is item 1 of the answer | The answer is built around "what the Hivemind entry shows" | General answer, then a separate "What our Hivemind says" section |
| New | General answer, then "In your org (an aside, …)" | General answer, then "Aside, in your org: …" | General answer, then "An aside from your org's Hivemind: …" |

The new text fixes the reported behaviour in the weaker Claude model, and Sonnet already did the right thing. All runs kept the incident's facts and its unverified status.

## Not changed

- The server `instructions` still say "keep untested ideas in self" without the `alternative` exception. Pi, Copilot CLI, Hermes, Cline, Junie and Antigravity do not show them anyway, and fixing them costs budget.
- The next release's CHANGELOG row must say that tool descriptions changed (the `hive_whoami` clause and three trims), and list the `payload_fields` warning.
