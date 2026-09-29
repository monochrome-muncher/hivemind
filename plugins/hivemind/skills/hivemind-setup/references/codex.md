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

**Where the key goes:** export `HIVEMIND_API_KEY` in the shell profile
that launches Codex (see "Shell profile" in the hivemind-setup skill).

The hivemind plugin (`codex plugin marketplace add
<git-url-of-the-hivemind-repo>`, then install `hivemind` from `/plugins`)
adds the skills and the startup hook; the server entry above is still
needed.

## Stay aware

- **Instruction block:** `~/.codex/AGENTS.md`.
- **Startup hook:** the hivemind plugin already has it. Without the
  plugin, put this in `~/.codex/hooks.json`, with the absolute path of
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
  approve it with `/hooks`.
- **Skills without the plugin:** copy both skill folders into
  `~/.agents/skills/`.

## Update

`codex plugin marketplace upgrade hivemind`, then reinstall with `codex
plugin add hivemind@hivemind` (Codex runs a cached copy), check with
`codex plugin list --marketplace hivemind`, then start a new session.

## Incognito

`hivemind-incognito codex` passes `-c mcp_servers.hivemind.enabled=false`.
It needs the `[mcp_servers.hivemind]` entry in `~/.codex/config.toml`
(Connect, above).
