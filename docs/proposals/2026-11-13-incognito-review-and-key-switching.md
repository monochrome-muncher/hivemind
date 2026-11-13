# Incognito review and switching agent keys (proposal, 2026-11-13)

Two questions from Monochrome: how does incognito work in every harness
today, and how can someone switch easily between agent keys, for example
to keep a confidential client's fleet apart from their everyday one?

Reviewed on main `cb62613` (2.25.1 plus test-only PRs). Builds on the
parked proposal `docs/proposals/2026-10-12-agent-key-switching.md`
(PR #156) instead of starting over.

**Short answer.**

- Incognito works as documented in the harnesses that were checked live.
  The real gap is the Cline and Kilo Code **editor extensions**: there is
  no launcher path for them, so incognito there is restraint only
  (finding 1). Cursor's incognito has never been run live. Two smaller
  fixes and one doc correction follow from the review.
- For switching, the parked recommendation still holds and is now
  stronger: a client-side `hivemind-as <profile> <harness>` launcher with
  named profiles, no server change and zero always-on bytes. Both open
  questions from October are answered (Claude Code checked live, Hermes
  from source). New: a small `hivemind-switch` skill, so the agent can
  guide the user, and a per-folder profile map, which matters more than
  it looked in October because local notes and harness memories are
  folder-scoped and can carry one fleet's knowledge into another.

## Part 1: incognito in every harness

One launcher, `plugins/hivemind/bin/hivemind-incognito`, sets
`HIVEMIND_INCOGNITO=1` (line 38) and applies each harness's own switch
(lines 44-245). ADR 0035 is the design; `DEVELOPING.md:594-648` the
reference table.

| Harness | Tools load? | Server contacted? | Key visible to the agent's shell? | How it is known |
|---|---|---|---|---|
| Claude Code | no (`deniedMcpServers` by name and URL, `bin:67-77`) | no | only if the key sits in the `env` block of `~/.claude/settings.json` (`DEVELOPING.md:634-636`) | live, 2.1.287 and 2.1.319 |
| Codex | no (`-c mcp_servers.hivemind.enabled=false`, `bin:93-96`) | no | no (key unset, URL kept) | live, 0.167.0 |
| Copilot CLI | no (`--disable-mcp-server hivemind`, `bin:106-107`) | no | no | live, 1.0.107 |
| Cursor | the `${env:…}` entry cannot authenticate; the always-on rule still loads (`bin:109-119`) | possibly, with an empty bearer | no | **docs only**: `bin:113` says "not yet checked live" |
| Gemini CLI (extension) | no (both set empty, `bin:137`) | no | no | live, 0.67.0 (PR #350) |
| Antigravity | listed, every call denied (`bin:208-214`) | **yes**, at startup with the stored key | no (key lives in Antigravity's config) | live, agy 1.5.3; documented in README |
| DeepSeek Harness | no (bundle row off) | no | no | live, 0.2.2-rc.1 |
| Hermes | no, after the one-time `enabled: ${HIVEMIND_ENABLED}` gate (`bin:140-155`) | no | **yes** (`~/.hermes/.env` is loaded over the environment, `hermes.md:78-85`) | live, October (ADR 0035 correction) |
| Junie | no (`--mcp-default-locations=false`, `bin:156-173`); also turns off the user's other MCP servers | no | no | live, 26.9.7 |
| Pi | no (extension registers nothing, blocks calls) | no | no | live |
| Oh My Pi | no (`HIVEMIND_MCP_URL=off`, `bin:174-184`) | no | no | live, 18.18.1 (PR #350) |
| OpenCode | no (plugin skips, overlay disables, `bin:185-198`) | no | **yes**, kept on purpose | live, 1.19.5 |
| KiloCode CLI | no (`bin:226-241`) | no | **yes**, kept on purpose | live, 7.8.27 |
| Cline | CLI: Hivemind never loads there anyway (`cline.md:35-38`). Extension: **no launcher path** | extension: yes | extension: yes | extension source only |

### Findings

1. **Cline and Kilo Code editor extensions have no incognito path
   (medium).** Hivemind works in the Cline VS Code / JetBrains extension
   but not in the `cline` CLI (`cline.md:35-38`). `hivemind-incognito
   cline` starts the CLI (`bin:216-225`), where nothing was on to begin
   with. The extension runs in the editor, and `hivemind-incognito code .`
   falls into the default branch (`bin:242-245`), which keeps the key and
   URL, so the extension connects. Kilo Code's extension is the same. The
   README presents Cline as "tools aren't loaded" (`README.md:698-701`).
   An editor that is already running probably ignores the new
   environment as well (known VS Code behaviour, not checked here).
   Fix: a `code`/`codium` branch that sets both variables **empty** (an
   override survives where an unset does not), plus honest README text
   about an editor that is already open. Needs a live check with VS Code.
2. **OpenCode and KiloCode keep the key for no reason (low).** In
   incognito the plugin returns before it reads the key
   (`opencode/hivemind.js:187-190` and `:271-274`), and a hand entry is
   disabled by the overlay, so nothing needs `HIVEMIND_API_KEY`. Leaving
   it means an agent with a shell could still call the REST API. Fix:
   unset both in those branches too. Needs a live check that a
   `{env:…}` hand entry that is disabled does not then fail config
   parsing.
3. **Hermes profiles are not checked (low).** The launcher checks
   `${HERMES_HOME:-~/.hermes}/config.yaml` and `.env` (`bin:144`,
   `bin:150`). Started as `hivemind-incognito hermes -p ops`, Hermes
   reads `~/.hermes/profiles/ops/`, so the warnings look at the wrong
   files. Only the notes are affected (`HIVEMIND_ENABLED=false` is still
   exported), but with key profiles this becomes the normal case. Fix:
   pick up `-p`/`--profile` the way Hermes does.
4. **A project can redirect Claude Code's key (low, now verified).**
   `claude-code.md:63-69` says a project `.claude/settings.json` `env`
   block "may feed" the plugin's `${HIVEMIND_MCP_URL}` ("not verified").
   Checked live with Claude Code 2.1.323: it does. With
   `{"env":{"HIVEMIND_MCP_URL":"<decoy>"}}` in the project, the plugin's
   server sent the user's key to the decoy, interactively right after
   "Yes, I trust this folder", and at once under `claude -p` (no trust
   prompt there). `claude mcp list` was not affected. This is the same
   trust boundary as a project's hooks, which can already run commands,
   so it is low; incognito is unaffected (denied by name). Fix: correct
   the reference text. The key-switching design below also closes it for
   profile sessions (`--settings` outranks project settings).
5. **Cursor incognito has never been run live (info).** `bin:113` and
   `cursor.md:182-192` are built from Cursor's docs. Worth one live run
   with `cursor-agent` (allowed by the network policy last time).
6. **Antigravity still connects (info, documented).** The server sees a
   connection with the stored key; calls are denied. The README says so
   and gives the `/plugin` workaround. Nothing to change.

## Part 2: switching agent keys

### What changed since October

- **Harnesses went from 9 to 14.** Cline, KiloCode and Cursor read
  `${env:…}`, so they fit the launcher. **Junie** needs a literal key in
  `~/.junie/mcp/mcp.json` (`junie.md:48-50`) and **Antigravity** keeps it
  in its own config, set in `/plugin` (`antigravity.md:46-50`, `:69-72`).
  Neither can be switched per session by a launcher.
- **Claude Code precedence, checked live (2.1.323)** with a mock MCP
  listener that logs the bearer:

  | Key in | Sent |
  |---|---|
  | launch shell only | shell key |
  | `~/.claude/settings.json` `env` only | settings key |
  | both | **settings key** (the shell loses) |
  | `settings.json` + `claude --settings <file>` with an `env` block | the `--settings` key, also with a shell key present |
  | project `.claude/settings.json` URL + `--settings <file>` with URL and key | the `--settings` URL and key (project loses) |

  So a launcher that only exports variables would silently do nothing
  for the recommended Claude Code setup. Passing a per-profile settings
  **file** works whatever the default setup is, keeps the key off the
  command line, and beats a hostile project's `env` block.
- **Hermes** has native profiles: `hermes -p <name>` (or `--profile`)
  sets `HERMES_HOME` to `~/.hermes/profiles/<name>` before anything loads
  (`hermes_cli/main.py:343-545` upstream, read from source, not run).
  Each profile has its own `.env`, so a Hivemind profile maps onto a
  Hermes profile.
- **The always-on budget is full**: 17,996 of 18,000 B after #417 (it was
  17,847 in October). Anything that must be always-on now needs a trim
  elsewhere. A new skill's body is not always-on; only its description
  shows in the skill list (outside `ALWAYS_ON_BUDGET`,
  `tests/unit/test_agent_plugin.py:462-475`).
- **Precedents and server support**: the `hivemind-update` skill
  (ADR 0132) is the model for a task skill; offboarding by owner alias
  (ADR 0135) already handles one person with several agents; a leaked
  profile key can be locked from the portal (ADR 0117) without touching
  the person's other profiles.

### Where each harness gets its key, and how to switch it

| Harness | Key comes from | Switch for one session | Verified |
|---|---|---|---|
| Claude Code (terminal) | `settings.json` `env` (wins) or shell | `claude --settings ~/.config/hivemind/claude.<p>.json` | live, 2.1.323 |
| Claude Code (desktop app, IDE) | `settings.json` only | none: edit the `env` block, restart | docs |
| Codex | env (`bearer_token_env_var`, `codex.md:15`) | export | docs, consistent with live runs |
| Copilot CLI | env in the header (`copilot-cli.md:22-26`); URL is fixed in the file | export (all profiles must use the same Hivemind URL) | docs |
| Cursor CLI / app | `${env:…}` (`cursor.md:30`) | export; the app only if started from that terminal and not already running | docs |
| Gemini CLI | env (`gemini-cli.md:49`) | export | docs |
| DeepSeek Harness | `~/.dsh/.env` or shell; exported wins (`deepseek-harness.md:21-35`) | export | docs |
| Hermes | `~/.hermes[/profiles/<p>]/.env`, loaded over env | `hermes -p <p>` | source |
| Pi, Oh My Pi | env | export | docs |
| OpenCode, KiloCode CLI | env (plugin) | export | docs |
| Cline, Kilo Code extensions | editor's env | start the editor from that terminal (same caveat as finding 1) | not checked |
| Junie | literal key in `~/.junie/mcp/mcp.json` | none: rebuild the file from the profile's key file (`junie.md:55-82`), restart | live (no `${VAR}` expansion, 26.9.7) |
| Antigravity | its own config, entered in `/plugin` | none: re-enter in `/plugin`, new session | live, 1.5.3 |

Per session from a terminal: 11 of 14. Persistent switch only: Junie,
Antigravity, and every GUI launch (Claude desktop app, IDE extensions,
DSH Desktop).

### Two things that are not about keys

Confidentiality between fleets is enforced by the server per key: an
agent key reads its own memories and its home fleet only, unless it is
`privileged`, which reads **every** fleet (`SPEC.md:440`). The leaks a
switch can cause are on the client:

- **The session's context.** Whatever fleet A's memories put into the
  conversation goes wherever the next write goes. Switching key inside a
  running session would carry it straight into fleet B. That is a second
  reason, besides the MCP connection being fixed at startup
  (`hivemind-setup/SKILL.md:30-31`), that a switch must be a new session.
- **Folder-scoped local memory.** The skill moves ordinary fallback notes
  into Hivemind "once you can" (`hivemind/SKILL.md:161-162`), under
  whichever identity is running then; harness memories (Claude Code's
  project memory, `AGENTS.md` notes) are per folder too. Fleet A notes
  written in `~/work/client` would be uploaded to fleet B the next time
  that folder runs under profile B. Only incognito notes are marked
  today.

The simplest defence for both is that a folder always runs under the same
profile, which is what option B gives.

### Option A: `hivemind-as` launcher with named profiles (recommended)

```sh
hivemind-as client claude
hivemind-as client codex "fix the flaky test"
hivemind-as --list            # profile names only, never keys
```

- Profile files in `~/.config/hivemind/` (folder 700, files 600):
  `<harness>.<profile>.env`, written by the user with the setup skill's
  no-echo `read` command. Claude Code gets `claude.<profile>.json` (an
  `env` block) because it must be passed with `--settings`. The existing
  `<harness>.env` and `settings.json` stay the default, so nothing
  changes for current users.
- Per harness, one `case` branch shaped like `hivemind-incognito`'s:
  source and export (most), `--settings <file>` (Claude Code), `-p
  <profile>` (Hermes). Junie and Antigravity print the persistent steps
  and stop, rather than rewriting config behind the user's back.
- Never prints the key, never puts it on a command line, and prints
  `hivemind-as: profile client` on stderr so the person sees which
  identity started.
- Composes with incognito (`hivemind-incognito` still unsets or denies).
- Each profile is just another registered agent. "One identity per
  harness" stays (`hivemind-setup/SKILL.md:233-242`): a profile is one
  agent per harness and fleet. The admin's **New agent** (ADR 0120) is
  the quickest way to get one, since `hive_register` needs the org key.
- Always-on cost: zero.

Cons: a restart per switch (unavoidable, see above); names multiply
(harness × profile); GUI launches stay on their default.

### Option B: profile by folder (recommended with A)

`hivemind-as auto <harness>`, or the shell functions the setup skill
already writes, pick the profile from a map the **user** owns,
`~/.config/hivemind/profiles`:

```
~/work/acme      client
~/src            dev
```

Longest prefix wins; no match means the default. Never read from the
repository: a repo file could pick the user's most privileged identity
(the risk the DSH resolver already guards against). A git-remote pattern
(`github.com/acme/*`) is a possible later extension.

It closes the folder-memory leak above without any always-on text, and it
means the person does not have to remember to switch. Cons: decided at
launch only; one more file.

### Option C: a `hivemind-switch` skill (recommended with A)

The agent cannot switch its own key, but it can make switching easy and
safe. A task skill like `hivemind-update`, with per-harness notes in the
existing `references/` files:

1. Say who you are now (`hive_whoami`: name, trust, home fleet).
2. List the profiles by file name (`ls ~/.config/hivemind/`), never
   reading a file.
3. Add a profile: ask the admin for an agent in the target fleet (or
   register with the org key), then give the user the no-echo command to
   write the profile file. The key never passes through the chat.
4. Switch: tell the user to end this session and start
   `hivemind-as <profile> <harness>`; for Junie, Antigravity and GUI apps,
   the persistent steps.
5. Confidentiality rules: start fresh in the other profile, do not carry
   a summary across, never open another profile's file, keep this
   identity's local notes where they are.

Cost: about 250 B of description in each session's skill list (outside
the always-on budget), zero always-on bytes. The alternative is a
"Switching identity" section in `hivemind-setup` and a few words added to
its description; cheaper by one description, but harder to find when the
user says "switch me to the client fleet". I'd make it its own skill, as
you suggested.

### Option D: hardening (pick any, later)

- **D1. Mark fallback notes with the identity**: `[hivemind: for
  <agent>]`, moved in only when `hive_whoami` names that agent. Closes the
  folder-memory leak even when one folder is used with two profiles.
  Costs about 80 always-on bytes, so it needs a trim. Not needed if B is
  used consistently.
- **D2. Keys from a password manager**: a profile file may hold
  `HIVEMIND_KEY_COMMAND="pass show hivemind/client"` (or macOS
  `security find-generic-password -w`) instead of the key. No plaintext
  keys on disk. Honest limit: an agent running as the same user can run
  that command too, unless the keychain asks every time.
- **D3. Docs**: for confidential fleets, give the person's agents
  `contributor`, never `privileged` (`SPEC.md:440`); a leaked profile key
  is locked from the portal (ADR 0117).

### Option E: link keys on the server (still not recommended)

Unchanged from October: it breaks one key per request (ADR 0031), a
leaked key would carry every linked identity, the model would pick the
identity per call (prompt injection can ask for the privileged one), and
it needs new tables, endpoints and always-on text the budget doesn't
have. New since then: it would also switch fleets inside one context,
which is exactly the leak above.

## Recommendation

1. Small incognito fixes as separate PRs: findings 1 and 2 (with live
   checks), 3, and the doc correction in 4; a live Cursor run (5).
2. Build A + B + C as one change: `bin/hivemind-as`, the
   `hivemind-switch` skill, per-harness notes, README section, an ADR.
   No server change, zero always-on bytes.
3. D3 in the same docs pass; D1 and D2 only if wanted later.

## Checked in this review

- Claude Code 2.1.323, plugin installed from this repo in an isolated
  home, a mock MCP server logging the `Authorization` header: the
  precedence table above and finding 4 (interactive run in a pty, trust
  prompt answered).
- Hermes `-p` from upstream source (`NousResearch/hermes-agent`,
  `hermes_cli/main.py`), not run.
- Not run here: Codex, Copilot, Gemini, Pi, OMP, OpenCode, KiloCode, DSH,
  Cursor, Junie, Antigravity, Cline (their key routes are documented and
  were live-checked on 2026-11-08; the launcher would get a live run per
  harness when built).
