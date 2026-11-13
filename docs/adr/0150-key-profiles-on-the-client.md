# One agent per fleet, chosen per session on the client: key profiles

Builds on [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md) (one agent, one key, one home fleet), [ADR 0031](0031-one-key-per-request-and-what-org-key-rotation-stops.md) (one key per request) and [ADR 0035](0035-incognito-sessions.md) (a client-side launcher with a branch per harness). Replaces the parked proposal `docs/proposals/2026-10-12-agent-key-switching.md`.

Status: Accepted.

## Context

A person who works in several fleets, for example their own team's and a confidential client's, needs one agent per fleet: a key reads its own home fleet only, unless it is `privileged`. Switching between those agents meant editing key files or shell functions by hand, so in practice people ran one agent everywhere, or a privileged one.

Two things leak between fleets on the client, not on the server. The conversation holds what one fleet's memories said, so a key switched inside a running session would carry that into the other fleet. Notes kept locally while Hivemind was unreachable, and a harness's own folder-scoped memories, are moved into Hivemind later by whichever agent runs in that folder next.

Checked live with Claude Code 2.1.323: a key in the `env` block of `~/.claude/settings.json` wins over the launching shell, so exporting another key changes nothing there, while `claude --settings <file>` with an `env` block wins over both and over a project's `.claude/settings.json`. Hermes has its own named profiles (`hermes -p <name>`, each with its own `.env`). Junie reads the key only as a literal in `~/.junie/mcp/mcp.json`, and Antigravity keeps it in its own settings.

## Decision

1. A **key profile** is a name on the user's machine for one of their agents, per harness. The server learns nothing about it: each profile is an ordinary registered agent, and every request still carries one key.
2. A launcher shipped in the plugin, `bin/hivemind-as <profile> <harness>`, starts a harness as that agent: it exports the URL and key from `~/.config/hivemind/<harness>.<profile>.env`, passes `~/.config/hivemind/claude.<profile>.json` to Claude Code with `--settings`, and starts Hermes with `-p <profile>`. It drops keys inherited from the shell first, refuses a profile file other users can read, never prints a key and never puts one on a command line. `default` is the user's existing setup, unchanged. It sets `HIVEMIND_PROFILE` so the agent can say which profile it started as.
3. **A switch is a new session.** Nothing switches a key inside a running session; the `hivemind-switch` skill says why and tells the user the command to restart with.
4. **The folder decides, through a file the user owns.** `hivemind-as auto <harness>` takes the profile from `~/.config/hivemind/profiles` (`<folder> <profile>` lines, longest folder wins, else `default`). A folder that always runs as one profile keeps its local notes and harness memories in one fleet. Nothing in a project can pick a profile: a repository must not choose which of the user's agents it gets.
5. Where a launcher cannot switch per session (Junie, Antigravity, desktop apps and editor windows already open), the skill gives the persistent steps, and the launcher refuses rather than rewriting a harness's config. DeepSeek Harness ignores the environment in a folder whose `.env` names `HIVEMIND_*` and falls back to `~/.dsh/.env`, so there the launcher refuses any profile but `default`.
6. A fourth skill, `hivemind-switch`, loaded on demand, guides the user: who am I, which profiles exist (by file name, never contents), how to add one (an agent from the admin's **New agent**, a profile file the user writes with the key typed, not pasted), and how to switch. It costs no always-on text.

## Considered and not done

- **Linking keys on the server**, so one key may act as several agents chosen per call. It breaks one key per request (ADR 0031), a leaked key would carry every linked identity, the model would choose the identity (a prompt injection could ask for the privileged one), and it switches fleets inside one context, which is the leak above.
- **A profile marker in the repository.** A cloned repository could pick the user's most privileged agent.
- **Marking local notes with the agent that wrote them.** It would close the folder leak even when one folder runs under two profiles, but costs always-on text the budget does not have; the folder map closes it for the usual case.

## Consequences

- Each harness needs a branch in `hivemind-as` and a row in the skill's table; names multiply (one agent per harness and fleet).
- Copilot CLI keeps its server address in its own config, so its profiles must share one Hivemind address.
- Editors reuse an open window's environment, so the user quits the editor before starting it under another profile.
