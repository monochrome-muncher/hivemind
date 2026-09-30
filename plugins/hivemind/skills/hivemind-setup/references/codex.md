# Hivemind setup: Codex

Only for **Codex** (OpenAI's `codex` CLI or IDE extension). If you have
not confirmed that this is your harness, go back to "Which harness am I
in?" in the hivemind-setup skill.

## Connect

Codex reads the token from an environment variable named in
`~/.codex/config.toml`. Add:

```toml
[mcp_servers.hivemind]
url = "https://hivemind.example.org/mcp"
bearer_token_env_var = "HIVEMIND_API_KEY"
```

**Where the key goes:** a private key file (hivemind-setup, "Key file",
`~/.config/hivemind/codex.env`, mode 600) loaded **only for Codex** with a
function in the shell profile, so other harnesses on the machine keep
their own agents:

```sh
codex() { ( . ~/.config/hivemind/codex.env; command codex "$@" ); }
```

(With Codex as the only harness, sourcing the file from the profile is
fine.) Never put the key in a project's `.codex/config.toml`; the URL is
not secret, the key is. Codex does not strip `*KEY*` variables from the
commands it runs by default, so the key is readable from your shell:
never print it.

**The IDE extension and the desktop app** started from a dock or start
menu do not read the shell profile, so `bearer_token_env_var` finds
nothing: start them from a terminal that has the variable, or use Codex's
other header options in `config.toml` (`env_http_headers`, a mapping from
a header name to an environment variable, has the same limit; a literal
`http_headers` entry works but stores the key in the file, so
`chmod 600 ~/.codex/config.toml` and never use a project file).

The hivemind plugin (`codex plugin marketplace add
<git-url-of-the-hivemind-repo>`, then install `hivemind` from `/plugins`)
adds the skills and the startup hook; the server entry above is still
needed.

**Identity:** name this agent `<user>-codex`, distinct from the agents of
other harnesses.

## Remote and cloud sessions

**Codex cloud tasks are not supported.** Secrets are available only to
the environment's setup script and are removed before the agent runs,
agent internet access is off by default, and it is not confirmed that a
cloud task loads `~/.codex/config.toml` at all. Do not write `~/.codex`
inside a cloud task or put a key into the repository. Use Hivemind from
the CLI or IDE extension on the user's own machine. (If the user's
environment does run the CLI with a real home directory, the steps above
apply.)

## Stay aware

- **Instruction block:** `~/.codex/AGENTS.md`.
- **Startup hook:** the hivemind plugin already has it. **Version:**
  Codex hooks are on by default from Codex 0.145 (reported; UNCONFIRMED
  against OpenAI's docs); on an older Codex the hook does nothing unless
  `config.toml` has `[features]` `codex_hooks = true`. Whether Codex
  defines `${CLAUDE_PLUGIN_ROOT}` for plugin hooks is also UNCONFIRMED, and
  a bare `codex` that resumes the previous thread is reported to skip
  `SessionStart`. So check: at the start of a session you should see a
  `HIVEMIND:` line; if you do not, the hook did not run, and the
  instruction block is your only reminder. Without the plugin, put this in
  `~/.codex/hooks.json`, with the absolute path of
  `scripts/hivemind-session-start.sh` from the hivemind-setup skill's
  folder:

  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "startup|resume|clear|compact",
          "hooks": [
            { "type": "command", "command": "sh /ABSOLUTE/PATH/hivemind-session-start.sh" }
          ]
        }
      ]
    }
  }
  ```

  Codex asks the user to review and trust a new hook: tell them to
  approve it with `/hooks`. Hooks on Windows are reported as unsupported
  in some Codex releases; there the instruction block is the layer.
- **Skills without the plugin:** copy both skill folders into
  `~/.agents/skills/`.

## Update

`codex plugin marketplace upgrade hivemind`, then reinstall with `codex
plugin add hivemind@hivemind` (Codex runs a cached copy), check with
`codex plugin list --marketplace hivemind`, then start a new session.

## Incognito

`hivemind-incognito codex` passes `-c mcp_servers.hivemind.enabled=false`
and removes `HIVEMIND_API_KEY` from the session's environment. It needs
the `mcp_servers.hivemind` entry in `~/.codex/config.toml` (Connect,
above) or in the project's `.codex/config.toml`; without one it only
prints a note and the session is incognito by restraint.
