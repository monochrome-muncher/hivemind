---
name: hivemind-switch
description: Switch which Hivemind agent (and so which fleet) the user's sessions run as, or add a profile for another fleet, keeping each fleet's keys and knowledge apart. Use when the user asks to switch fleet, agent, key or profile, or to work for a different team or client.
---

# Hivemind switch

A person who works in several fleets, for example their own team and a
confidential client, has one Hivemind agent per fleet, each with its own
key. A **profile** is a name for one of those agents ("client", "ops").
The server keeps fleets apart by key; your job is to keep them apart on
this machine.

**You cannot switch in this session.** The MCP connection reads its key
when the harness starts, and this conversation already holds what the
current fleet told you: carried into another fleet, that is a leak. A
switch is always a new session, started by the user.

**Ground rules:**

- **Never read, print or copy a key**, and never open a profile file:
  list them by name only. The user types keys themselves.
- **Carry nothing across.** Don't summarize this session for the next
  one, and don't write what you learned here into another fleet.
- **Local notes stay with their profile.** Notes kept in this folder
  while Hivemind was unreachable belong to the agent that wrote them; if
  you find notes and `HIVEMIND_PROFILE` (or `hive_whoami`) shows another
  agent than the one that wrote them, ask the user before moving any in.

## 1. Where am I?

Call `hive_whoami`: name, trust level and home fleet. `HIVEMIND_PROFILE`
in your environment names the profile the session started with (unset:
the user's usual setup). Tell the user both, and that a `privileged`
agent reads every fleet, so a confidential fleet's people should run
`contributor` agents.

## 2. Profiles on this machine

```sh
hivemind-as --list
```

prints `<profile> (<harness>)` pairs, never keys. The launcher is
`bin/hivemind-as` in the plugin folder (two levels above this skill's
folder); if it is not on `PATH`, give its full path, or suggest
`ln -s <plugin>/bin/hivemind-as ~/.local/bin/`. Without it,
`ls ~/.config/hivemind/` shows the same file names.

## 3. Add a profile

1. **An agent in the target fleet.** The quickest way is the admin's
   **New agent** in the admin panel (name, owner alias, trust level, home
   fleet; it shows the key once). Registering from a session that has the
   org key works too (hivemind-setup, "Register"). Suggest a name such as
   `<user>-<harness>-<fleet>`: one agent per harness and fleet.
2. **The profile file**, written by the user with the key typed, not
   pasted into this chat. Replace `<harness>` and `<profile>`:

   ```sh
   mkdir -p ~/.config/hivemind && chmod 700 ~/.config/hivemind
   printf 'Hivemind key: '; stty -echo; read -r k; stty echo; echo
   ( umask 077; printf 'export HIVEMIND_MCP_URL="%s"\nexport HIVEMIND_API_KEY="%s"\n' \
       "https://hivemind.example.org/mcp" "$k" > ~/.config/hivemind/<harness>.<profile>.env ); unset k
   ```

   `<harness>` is `codex`, `copilot`, `cursor`, `gemini`, `dsh`, `pi`,
   `omp`, `opencode`, `kilocode`, or `code` for the Cline and Kilo Code
   editor extensions. Exceptions:

   - **Claude Code**: `~/.config/hivemind/claude.<profile>.json`, mode
     600, holding `{"env": {"HIVEMIND_MCP_URL": "…", "HIVEMIND_API_KEY": "…"}}`.
     The launcher passes it with `--settings`, which wins over the key in
     `~/.claude/settings.json` and over a project's settings.
   - **Hermes**: a Hermes profile of the same name (`hermes profile
     create <profile>`), with the two variables in
     `~/.hermes/profiles/<profile>/.env` (mode 600), and the hivemind
     server entry in the root or the profile's `config.yaml`.
   - **Copilot CLI**: the server address is fixed in
     `~/.copilot/mcp-config.json`, so every profile must use the same
     Hivemind address.

3. **Optional: a folder map**, so the right profile starts by itself.
   The user owns `~/.config/hivemind/profiles`, one `<folder> <profile>`
   per line:

   ```
   ~/work/acme   client
   ~/src         dev
   ```

   `hivemind-as auto <harness>` uses the longest folder that holds the
   current one, else the default. A folder always running as the same
   profile is also what keeps its local notes and harness memories in one
   fleet. Never suggest a file inside a project for this: a repository
   must not choose which of the user's agents it gets.

## 4. Switch

Tell the user to end this session and start the harness through the
launcher, from a terminal:

```sh
hivemind-as client claude      # or: hivemind-as auto claude
```

`default` is their usual setup. With the folder map, a shell function
makes it automatic, for example `codex() { hivemind-as auto codex "$@"; }`
in the shell profile (instead of the one hivemind-setup wrote).

Where a launcher cannot switch, the change lasts until switched back;
say so, and that a running session keeps its old key until restarted:

| Harness | Switch |
|---|---|
| Junie | rebuild `~/.junie/mcp/mcp.json` from the profile's key file (the command in hivemind-setup's `references/junie.md`, with `junie.<profile>.env`), then restart |
| Antigravity | `/plugin` > hivemind > "Configure MCP servers…", enter the other key, new session |
| Claude desktop app, IDE extensions | the `env` block of `~/.claude/settings.json`, then restart |
| VS Code (Cline, Kilo Code), Cursor app | quit the editor, then start it with `hivemind-as <profile> code .` (or `cursor .`): an open window keeps its environment |
| DeepSeek Harness | the launcher refuses in a folder whose `.env` names `HIVEMIND_*` (DSH would fall back to `~/.dsh/.env`, the default agent) |

After the switch, the new session's `hive_whoami` should show the other
agent; if not, the key did not reach the harness (hivemind-setup, "Still
the org key?").
