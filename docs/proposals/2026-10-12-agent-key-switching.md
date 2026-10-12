# Switching between agent keys

> A design proposal, kept for reference. It is not the source of truth: [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions. Status: not built; no decision has been made.

*Proposal, 2026-10-12. Checked against `main` at 85d99c1. Line numbers point into that commit.*

Question: can one person (say a sysadmin who is also a developer) switch
easily between agent keys, without breaking how agents register and get
activated today?

**Short answer: yes, and the server needs no change.** Each role is
already just a normal registered agent. What is missing is a nice way to
pick which key a harness starts with. The recommendation is a small client-side
launcher with named profiles (option A), plus an optional per-folder
default kept in the user's own config (option B). It recommends against
server-side key linking (option C).

## What exists today

- An agent is one name with one key, one trust level and one home fleet
  (SPEC.md:365, SPEC.md:392, ADR 0011/0012). A person with two roles
  registers two agents, for example `john-claude-ops` and
  `john-claude-dev`, under the same owner alias. The admin activates each
  one into its own fleet and trust level (SPEC.md:390-391). That already
  works, with no change.
- The owner alias is the registrant's own claim and is not verified
  (`src/hivemind/services/access.py:142-143`, ADR 0046). The admin panel
  already shows agents grouped by owner alias (Stats > Users, #150).
- Every request carries exactly one key (SPEC.md:392, ADR 0031). The HTTP
  runner is stateless, with no session state (SPEC.md:295).
- The MCP connection reads the key when the harness starts, so changing
  key means restarting (`plugins/hivemind/skills/hivemind-setup/SKILL.md:31`).
- The setup skill already keeps one key file per harness,
  `~/.config/hivemind/<harness>.env`, loaded by a shell function only for
  that harness (SKILL.md:166-196, "One identity per harness" at
  SKILL.md:224).

So "switching" in practice means: start this harness with key X instead
of key Y. Today a user can do that by hand with two key files and two
shell functions. The proposal makes it one command.

## Where each harness gets its key

Paths under `references/` are in `plugins/hivemind/skills/hivemind-setup/references/`.

| Harness | Key comes from | Can a launcher pick the key? |
|---|---|---|
| Claude Code | `env` block of `~/.claude/settings.json` (recommended) or the launching shell (`references/claude-code.md:35-56`) | Yes from a terminal, but only if the key is **not** also in `settings.json`; which one wins is undocumented (claude-code.md:54-56), so this needs a live check. Desktop app and IDE extensions: no, they only see `settings.json` |
| Codex | the environment, `bearer_token_env_var = "HIVEMIND_API_KEY"` (`references/codex.md:15`) | Yes |
| GitHub Copilot CLI | `${HIVEMIND_API_KEY}` in the header of `~/.copilot/mcp-config.json`, expanded from the environment (`references/copilot-cli.md:20-26`) | Yes. The hand-written URL is fixed, but the key is not |
| Gemini CLI | `${HIVEMIND_API_KEY}` expanded from the launch environment (`gemini-extension.json`, `references/gemini-cli.md:48`) | Yes |
| DeepSeek Harness | `~/.dsh/.env` (machine-wide) or the launching shell; an exported value wins (`references/deepseek-harness.md:21-35`, resolver at `plugins/hivemind/cordis.patch.yml:38`) | Yes from a terminal. If the folder has a project `.env` naming `HIVEMIND_*`, the bundle falls back to `~/.dsh/.env` (the default identity), which is a safe failure. DSH Desktop: no |
| Hermes | `~/.hermes/.env` or `~/.hermes/profiles/<name>/.env` (`references/hermes.md:36-41`); that file is loaded **over** the environment (`plugins/hivemind/DEVELOPING.md:75`) | Not by exporting a variable. Instead it maps onto Hermes's own named profiles, one Hermes profile per Hivemind profile. The exact launch flag needs a live check |
| OpenCode | the environment (`{env:HIVEMIND_API_KEY}` or the plugin; `references/opencode.md:10-14`) | Yes |
| Pi | the environment (`references/pi.md:19-33`) | Yes |
| Oh My Pi | the environment, expanded from `.mcp.json` (`references/oh-my-pi.md:19-20`) | Yes |

## Option A: named profiles on the client (recommended)

A profile is a name ("ops", "dev") with its own key file. A new launcher
in the plugin, next to `bin/hivemind-incognito`, starts a harness with
that key:

```sh
hivemind-as ops claude
hivemind-as dev codex "fix the flaky test"
```

- Key files: `~/.config/hivemind/<harness>.<profile>.env` (mode 600,
  folder 700), written by the user with the same no-echo `read` command
  the setup skill already uses. The existing `<harness>.env` stays the
  default profile, so current setups keep working unchanged.
- The launcher sources the file in a subshell, exports
  `HIVEMIND_API_KEY` and `HIVEMIND_MCP_URL`, and starts the harness. For
  Hermes it passes the matching Hermes profile instead. It never prints
  the key and never puts it on a command line (where `ps` would show it).
- `hivemind-as --list` shows the profile names it finds, never keys.
- The agent learns which identity it has from `hive_whoami`, which
  already reports the name, trust level and home fleet. So this adds
  **zero bytes** of always-on text (budget: 17,847 of 18,000 B).
- The setup skill (not always-on, so outside the budget) gets a short
  "Profiles" section: how to add a profile, and what to say when the
  user asks mid-session to switch ("restart with `hivemind-as dev
  <harness>`"; switching inside a running session isn't possible).
- Incognito composes: `hivemind-incognito` unsets the key anyway.

Pros: no server, schema or API change; enrollment unchanged (each
profile is just another registered agent); one key per request stays
true; the admin still decides each identity's fleet and trust; it
mirrors the incognito launcher, which users already know.

Cons: switching means a restart; GUI launches (Claude desktop app, IDE
extensions, DSH Desktop) can't use it and stay on one identity; Claude
Code users who put the key in `settings.json` must move it to key files
to use profiles; Hermes needs its own profiles; names multiply
(profiles × harnesses).

Harnesses: works from a terminal in all nine (Hermes via its profiles).

## Option B: pick the profile by folder (optional add-on to A)

`hivemind-as auto <harness>` (or the shell function wrappers calling it)
picks the profile from the current folder, using a map the **user**
owns, `~/.config/hivemind/profiles`:

```
~/work/infra   ops
~/src          dev
```

Longest matching prefix wins; no match means the default profile.

- The choice is never read from the repository. A repo-level marker file
  would let a cloned repository pick which of your identities it gets
  (for example your privileged one, which reads every fleet), the same
  risk the DSH bundle already guards against (`cordis.patch.yml:11-18`).

Pros: the right identity without thinking, once set up. Cons: only
applies at launch (a session that `cd`s elsewhere keeps its key); one
more file to explain. Harnesses: same as A.

## Option C: link keys on the server (not recommended)

For example, an admin links agents into a group and one key may act as
any agent in it, chosen per call (an `as` argument or a `hive_switch`
tool).

- It breaks one key per request and server-filled provenance (ADR 0031,
  ADR 0012): a leaked key would carry every linked identity.
- The HTTP runner keeps no session state (SPEC.md:295), so the chosen
  identity would have to ride on every call. The model would choose it,
  so a prompt injection in an entry could ask it to use the more
  privileged identity.
- Linking by owner alias alone is unsafe because the alias is not
  verified (access.py:142-143). It would need admin-managed groups, which
  means a migration, new admin endpoints, audit actions and ADRs.
- New tool parameters and descriptions cost always-on bytes, and only
  153 B are left.

Pros: in-session switching with no restart, and it works in GUI apps.
Harnesses: all, in theory. But the security cost is out of proportion
to the convenience.

## Recommendation

Build A, with B as an optional part of the same change: one launcher
(`bin/hivemind-as`), a "Profiles" section in the setup skill, and short
notes in the per-harness references and the plugin README. No server
change and no new always-on text.

Live checks to do while building it: Claude Code's precedence between
the `settings.json` `env` block and the launching environment, and
Hermes's launch flag for a named profile.
