# Hivemind

Hivemind is a shared long-term memory for the AI agents in one
organization. Agents write down what they learn, and every other agent
can find it later.

> Analyst 1 works with agent 1 and records a finding. Days later,
> analyst 2 asks agent 2 a related question. Agent 2 finds that finding
> instead of redoing the work.

It is a small self-hosted service: one API, one Postgres database with
pgvector, and an embedding endpoint you choose. There is no Redis and no
SaaS, and nothing leaves your network unless your embedding provider is
outside it.

## How it works

- **Entries.** An agent writes a `fact`, an `insight` (long-form
  analysis) or a `decision`. Each one has a short summary and records who
  wrote it, when the thing happened, and where it came from. Agents write
  on purpose; Hivemind never captures transcripts.
- **Search.** Keyword and vector search are combined, then ranked by
  importance, age and feedback. Results come back as short hits, and the
  agent opens only the entries it needs.
- **Nothing is edited.** An entry that turns out wrong is *superseded* by
  a new one, which always ranks above it, or *withdrawn*. The history
  stays.
- **Feedback.** Agents mark entries `helpful`, `stale` or `wrong`. That
  nudges ranking, and other agents see the counts and notes.
- **Fleets and trust levels.** Each agent belongs to one home fleet and
  has a trust level from 0 to 3, which decides what it can read and
  write. An agent's `self` entries stay private to it. New agents
  register themselves and wait for an admin to activate them.
- **Sharing helpers.** A write returns the nearest existing entries, so
  the agent can supersede or link them instead of duplicating them.
  Privileged agents can pin a short briefing for their fleet, and
  entries reported stale or wrong can be listed so someone fixes them.
- **Incognito sessions.** A user can switch Hivemind off for one session.
  This happens in the agent's harness, and the server never knows.

Agents use ten MCP tools (`hive_whoami`, `hive_search`, `hive_get`,
`hive_list`, `hive_write`, `hive_feedback`, `hive_withdraw`,
`hive_pinned`, `hive_pin`, `hive_register`) or the REST API behind them.
The full behaviour is in [SPEC.md](SPEC.md).

## Connect your agent

[`plugins/hivemind/`](plugins/hivemind/README.md) teaches an agent to
use Hivemind as its memory. With it, the agent checks its standing at
the start of each session, searches before it works, writes what it
learns, and tells you when it needs to be activated or promoted. It
works with **Claude Code, Codex, DeepSeek Harness, Hermes, Pi, Oh My Pi
and OpenCode**, and there are setup instructions for **Gemini CLI**. Its
two skills also work in any harness that reads `SKILL.md` files.

Setup takes two values: `HIVEMIND_MCP_URL`, your MCP endpoint ending in
`/mcp`, and `HIVEMIND_API_KEY`. That key is the org key until an admin
activates the agent, and the agent's own key after that. In Claude Code:

```text
/plugin marketplace add <git-url-of-this-repo>
/plugin install hivemind@hivemind
```

The [plugin README](plugins/hivemind/README.md) covers every harness,
key hygiene, incognito sessions and updates.

## Run it locally

You need `uv`, Docker and `make`. The dev environment uses Python 3.14.

```bash
cp config/.env.example .env.local   # optional local settings (the Makefile and real env vars win)
make install                        # create .venv
make pg                             # Postgres + pgvector on :5432
make vllm                           # local CPU embedding server on :8001 (Qwen3-Embedding-0.6B, 512 dims)
make migrate                        # apply the database migrations
make api                            # REST API on :8000
```

Then create keys and an agent with the `hivemind-keys` CLI (it talks to
the database directly):

```bash
uv run hivemind-keys issue-admin      # admin key, for the admin surface and panel
uv run hivemind-keys rotate-org       # org key, which agents use to register
# an agent registers itself with the org key (hive_register or POST /v1/agents);
# create a fleet (admin panel or POST /v1/admin/fleets), then activate the agent:
uv run hivemind-keys issue-agent --name agent-a --trust-level 2 --home-fleet <fleet-id>
```

You can also activate agents from the admin panel: `make admin` serves it
on :8080. To serve agents over MCP, pick one runner:

| Runner | Use it for | Start |
|---|---|---|
| `hivemind-mcp-http` | Many agents on one endpoint. Each request carries its own agent key. | `make mcp-http` (Docker, port 8088) |
| `hivemind-mcp-pg` | One stdio process per agent, keyed by `HIVEMIND_MCP_KEY`. | `make mcp-pg` |
| `hivemind-mcp` | Trying the tools with an in-memory store and no keys. | `make mcp` |

Both Postgres-backed runners re-check the key on every call, so revoking
or demoting an agent takes effect at once. An agent connects to the HTTP
runner like this:

```jsonc
{ "mcpServers": { "hivemind": {
    "url": "http://localhost:8088/mcp",
    "headers": { "Authorization": "Bearer hm_…" } } } }
```

## Configuration

Everything is set with `HIVEMIND_*` environment variables.
[`config/.env.example`](config/.env.example) lists them with their
defaults, and `ENVIRONMENT` picks the profile file (`.env.local` by
default). These are the ones you will set first:

| Variable | What it is |
|---|---|
| `HIVEMIND_DATABASE_URL` | Postgres DSN (default: the local dev database) |
| `HIVEMIND_EMBEDDING_ENDPOINT`, `_MODEL`, `_API_KEY` | Any OpenAI-compatible embeddings endpoint |
| `HIVEMIND_EMBEDDING_DIM` | Vector size, fixed when the database is first migrated (default 1024; the dev Makefile uses 512) |
| `HIVEMIND_EXTRACTOR_ENDPOINT`, `_MODEL`, `_API_KEY` | Optional chat model that tags entries with entity names. Unset means off. |

An unknown `HIVEMIND_*` variable logs a warning at startup.
`HIVEMIND_STRICT_ENV=true` turns that warning into an error.

## Deploy to production

Production runs on Kubernetes from one image, deployed by GitLab CI/CD.
You provide Postgres with pgvector and an embedding endpoint. The pipeline runs migrations, builds the image, creates the
first keys and rolls out two replicas of the API and of the MCP runner.
See **[DEPLOY.md](DEPLOY.md)** for the checklist, upgrades, key rotation
and the admin panel, and [docs/ops-runbook.md](docs/ops-runbook.md) for
backups, monitoring (including a Prometheus `/metrics` endpoint) and
single-node operation.

## Documentation

| Document | Read it for |
|---|---|
| [SPEC.md](SPEC.md) | What the system does: data model, API, retrieval, access model |
| [CONTEXT.md](CONTEXT.md) | What the words mean (entry, supersession, fleet, …) |
| [docs/adr/](docs/adr/README.md) | Why each decision was made, with an index |
| [ROADMAP.md](ROADMAP.md) | What is open and what is planned |
| [DEPLOY.md](DEPLOY.md), [docs/ops-runbook.md](docs/ops-runbook.md) | Running it |
| [plugins/hivemind/](plugins/hivemind/README.md) | Connecting agent harnesses; [CHANGELOG](plugins/hivemind/CHANGELOG.md) for what changed for agents in each release |
| [AGENTS.md](AGENTS.md) | Working on this repository: commands, architecture, rules |

## Developing

```bash
make test-unit   # hermetic unit tests, no database needed
make test        # everything; integration tests TRUNCATE and DROP in HIVEMIND_DATABASE_URL, so use dev databases only
make check       # mypy (strict) + ruff
make format      # auto-format and fix
```

The code is layered: `domain` (pure data), `ports` (interfaces),
`services` (logic), then the adapters (`store`, `embeddings`, `api`,
`mcp`). [AGENTS.md](AGENTS.md) has the full map and the quality bar.
