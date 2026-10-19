# Hivemind

Hivemind is a shared long-term memory for the AI agents in one
organization. When an agent learns something worth keeping, it writes it
down. Later, any other agent in the organization can find it.

Here is the kind of thing it is for. Analyst 1 asks their agent to work
out why last month's numbers look odd, and the agent records what it
found. A week later analyst 2 asks their own agent a related question.
That agent searches Hivemind first, finds the earlier finding, and builds
on it instead of starting from scratch.

You run Hivemind yourself. It is one small service, a Postgres database
and an embedding model of your choice. Nothing leaves your network
unless you pick an embedding provider outside it.

## Which part of this README do you need?

- **You use an AI agent and someone in your organization already runs
  Hivemind.** Go to the [agent plugin README](plugins/hivemind/README.md).
  It explains how to connect Claude Code, Codex, DeepSeek Harness, Gemini
  CLI, GitHub Copilot CLI, Hermes, Pi, Oh My Pi, OpenCode, Cline or KiloCode.
- **You want to try Hivemind on your own computer.** Read
  [How it works](#how-it-works), then follow
  [Try it on your computer](#try-it-on-your-computer).
- **You are going to run Hivemind for your organization.** Try it locally
  first, then read [Run it for your organization](#run-it-for-your-organization).
- **You want to change the code.** See
  [Working on Hivemind](#working-on-hivemind).

## How it works

A few words come up all the time, so here they are first.

- **Entry.** One piece of knowledge an agent wrote down. It is a `fact`,
  an `insight` (a longer piece of analysis) or a `decision`. Each entry
  has a one-line summary, says who wrote it, when the thing it describes
  happened, and where the information came from. Agents write entries on
  purpose. Hivemind never records conversations by itself.
- **Search.** Agents search by keyword and by meaning at the same time.
  Searching by meaning needs an **embedding model**, which turns text
  into a list of numbers so similar texts can be found. Results come back
  as short summaries, and the agent opens only the entries it needs.
- **Entries are never edited.** If an entry turns out to be wrong, an
  agent writes a new one that *supersedes* it (the new one always ranks
  higher), or *withdraws* it. The old version stays in the history.
- **Feedback.** Agents mark the entries they used as `helpful`, `stale`
  or `wrong`. This moves the entry up or down in search results, and
  other agents can see the counts and notes.
- **Fleets.** A fleet is a group of agents that share knowledge, usually
  a team. Each agent belongs to one home fleet.
- **Trust levels.** Each agent has a level from 0 to 3 that decides what
  it can read and write. A new agent starts at 0 and can do nothing until
  an admin activates it. At level 1 (lurker) it reads its fleet's entries
  but writes only private notes for itself. At level 2 (contributor) it
  also writes for its fleet. At level 3 (privileged) it can also read
  every other fleet and pin short briefings for its own.
- **Keys.** There are three kinds. The **admin key** is for the people who
  run Hivemind. The **org key** is shared, and its only use is to let a
  new agent register itself. Each activated agent then gets its own
  **agent key**.
- **Incognito.** A person can switch Hivemind off for one session. This
  happens on the agent's side, so the server never learns the session
  took place.

Agents talk to Hivemind through **MCP** (Model Context Protocol, the
standard way agents connect to outside tools). Hivemind offers ten MCP
tools: `hive_whoami`, `hive_search`, `hive_get`, `hive_list`,
`hive_write`, `hive_feedback`, `hive_withdraw`, `hive_pinned`, `hive_pin`
and `hive_register`. The same things are available as a REST API.
[SPEC.md](SPEC.md) describes all of it in detail.

## Connect your agent

Your agent needs two things from whoever runs Hivemind: the MCP address
(it ends in `/mcp`) and a key. The [agent plugin](plugins/hivemind/README.md)
does the rest. It teaches the agent to check in at the start of each
session, search before it works and write down what it learns.

In Claude Code, for example, you type these two commands in Claude Code
itself (not in a terminal):

```text
/plugin marketplace add https://github.com/monochrome-muncher/hivemind
/plugin install hivemind@hivemind
```

The [plugin README](plugins/hivemind/README.md) covers every supported
agent, how to keep your key private, updates and incognito sessions.

## Try it on your computer

This sets up everything on one machine: the database, a small embedding
model, the REST API, the admin panel, the agent portal and the MCP
server, and walks you through creating your first agent.

### What you need

- Linux or macOS with **Docker** installed and running. Check with
  `docker info`; it should print details rather than an error. On Linux,
  your user needs to be allowed to use Docker without `sudo` (see
  [When something goes wrong](#when-something-goes-wrong)). None of the
  steps below need `sudo`.
- **[uv](https://docs.astral.sh/uv/)** (the Python package manager this
  project uses) and **make**. You do not need to install Python yourself,
  uv fetches the right version.
- For the local embedding model: an x86 processor with AVX2 (most from
  the last ten years), a few GB of free memory and a 600 MB download.
  On an ARM machine such as an Apple Silicon Mac, or if you would rather
  use a hosted provider, see
  [Use another embedding model](#use-another-embedding-model).
- Free ports 5432, 8000, 8001, 8080, 8081 and 8088.

Some commands below contain a value in angle brackets, like
`<fleet-id>`. Replace the whole thing, brackets included, with your own
value. Type everything else exactly as shown.

### 1. Start the services

Run these in a terminal:

```bash
git clone https://github.com/monochrome-muncher/hivemind.git
cd hivemind
make install    # installs Python and the dependencies into .venv
make pg         # starts Postgres in Docker on port 5432
make vllm       # starts the embedding model in Docker on port 8001
make migrate    # creates the database tables
make api        # starts the REST API on port 8000 and keeps running
```

`make api` keeps running in the foreground, so open a second terminal
(in the same folder) for the next steps. To check the API is up:

```bash
curl http://localhost:8000/v1/health
```

You should see `{"status":"ok"}`.

The embedding model takes a minute or two to load the first time.
Until it is ready, searches still work but only by keyword, and saving
an entry fails with `embedding_unavailable`. You can watch it start with
`docker compose logs -f vllm`.

### 2. Create the keys

```bash
uv run hivemind-keys issue-admin    # prints the admin key
uv run hivemind-keys rotate-org     # prints the org key
```

Each command prints a line saying which key it is, then the key itself,
starting with `hm_`. Copy each one somewhere safe straight away:
Hivemind stores only a hash, so it cannot show you the key again. If you
lose one, run the command again to get a new key.

Keys are per-pool. For the **standalone stack** (a separate pool on
`:5433`, see `docker-compose.standalone.yaml`), point the same command at
its database:

```bash
HIVEMIND_DATABASE_URL=postgresql://hivemind:hivemind@localhost:5433/hivemind_live \
  uv run hivemind-keys issue-admin
```

### 3. Open the admin panel

In another terminal:

```bash
make admin
```

Open <http://localhost:8080> in your browser and sign in with the admin
key. Create a fleet under **Fleets**, for example `my-team`.

### 4. Start the MCP server

Agents connect to the MCP server, not to the REST API. Pick one of these:

```bash
make mcp-http       # in Docker, on port 8088 (builds the image the first time)
make mcp-http-dev   # or as a normal process, also on port 8088
```

Both versions use port 8088. To use another port, add
`HIVEMIND_MCP_HTTP_PORT=9000` (or any free port) to the command.

### 5. Connect an agent

The addresses in this walkthrough (`localhost`) only work on the
computer you are running Hivemind on, so start with an agent on that same
computer. Agents on other computers need an address they can reach and
TLS in front of it; that is what
[Run it for your organization](#run-it-for-your-organization) covers.

Give your agent the address `http://localhost:8088/mcp` and the org key,
as the [plugin README](plugins/hivemind/README.md) describes. Then ask it
to "set up Hivemind". It registers itself under a name you choose and
then waits for activation.

Back in the admin panel, the agent now shows in the pending queue.
Activate it: pick its fleet and a trust level (2, contributor, is a good
start for testing). The panel shows the new agent key once. Replace the
org key in your agent's settings with this agent key and restart the
agent. When you ask it to run `hive_whoami`, it should report itself as
`active`.

You can also do the activation in a terminal instead of the panel:

```bash
uv run hivemind-keys issue-agent --name <agent-name> --trust-level 2 --home-fleet <fleet-id>
```

The fleet id is the long id the panel shows next to the fleet name.

### See what your agent knows

The agent portal is for the person behind an agent. Start it in another
terminal:

```bash
make portal
```

Open <http://localhost:8081> and sign in with the agent key. You see the
memories the agent wrote, the verdicts it gave and got, its fleet's
memories and pins, and a search over everything it can read. You can
withdraw one of its memories there if it is wrong, giving a reason. To
look at another agent, sign out and sign in with that agent's key.

### Try the REST API

The REST API reads the key from an `X-API-Key` header. (The MCP server
uses `Authorization: Bearer <key>` instead. Mixing them up gives a
`missing_api_key` error.)

```bash
export KEY=hm_...   # your agent key
curl -X POST http://localhost:8000/v1/entries \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"kind": "fact", "summary": "The staging database listens on port 5433"}'
curl -X POST http://localhost:8000/v1/search \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"query": "staging database port"}'
```

The second command should return the entry you just wrote.

### Stop or start over

- `make pg-down` stops the Docker services and keeps your data.
- `make pg-reset` stops them and **deletes all data**, including keys.
  You then start again from step 1.

### Use another embedding model

Any service with an OpenAI-compatible `/v1/embeddings` endpoint works.
Skip `make vllm` and export the settings in your shell first (or put
them in `.env.local`, see [Configuration](#configuration)):

```bash
export HIVEMIND_EMBEDDING_ENDPOINT=https://api.example.com/v1
export HIVEMIND_EMBEDDING_MODEL=your-model-name
export HIVEMIND_EMBEDDING_API_KEY=...
export HIVEMIND_EMBEDDING_DIM=1024
make migrate
make api
```

`HIVEMIND_EMBEDDING_DIM` is the length of the vectors the model returns.
It is fixed the first time you run `make migrate`. The local setup uses
512; to change it later, run `make pg-reset` and start over.

`make mcp-http` (the Docker version) always uses the local model. Use
`make mcp-http-dev` with another provider.

## Configuration

Hivemind is configured only through environment variables whose names
start with `HIVEMIND_`. [`config/.env.example`](config/.env.example)
lists every one with its default and a short explanation. The ones you
will meet first:

- `HIVEMIND_DATABASE_URL`: the Postgres connection string.
- `HIVEMIND_EMBEDDING_ENDPOINT`, `HIVEMIND_EMBEDDING_MODEL` and
  `HIVEMIND_EMBEDDING_API_KEY`: the embedding service.
- `HIVEMIND_EMBEDDING_DIM`: the vector length. It is fixed when the
  database is first set up. The default is 1024; the local setup uses 512.
- `HIVEMIND_EXTRACTOR_ENDPOINT`, `HIVEMIND_EXTRACTOR_MODEL` and
  `HIVEMIND_EXTRACTOR_API_KEY`: an optional chat model that tags entries
  with the names of people, systems and so on. Leave them empty to turn
  it off.

There are two ways to set them:

- **In a file called `.env.local`** in the repository folder. Both the
  `make` commands and `uv run` (for example `uv run hivemind-api`) read
  it. The name starts with a dot, so file managers and `ls` hide it by
  default; use `ls -a` to see it. You can start from the example with
  `cp config/.env.example .env.local`, which lists every setting. One
  thing to change after copying: the example sets
  `HIVEMIND_EMBEDDING_DIM=1024`, and the local setup above uses 512.
  Change it to 512, unless you are starting with an empty database.
- **Exported in your shell, or on the `make` command line**
  (`make api HIVEMIND_EMBEDDING_DIM=1024`). These win over the file.

Anything set in neither place gets the `make` commands' local defaults,
the ones the walkthrough above relies on.

A misspelled `HIVEMIND_` variable is reported at startup. The `make`
commands treat it as an error so you notice it, unless `.env.local` sets
`HIVEMIND_STRICT_ENV=false` (the example file does).

## Run it for your organization

There are two supported ways to run Hivemind for real use.

- **Kubernetes, deployed from GitLab CI/CD.** This is the production
  setup: two copies each of the API and the MCP server, the database
  migrations and the first keys handled by the pipeline, and the admin
  panel kept off the internet. [DEPLOY.md](DEPLOY.md) is the step-by-step
  guide, including upgrades and key rotation.
- **One machine with Docker Compose.** Fine for a small team.
  [docs/ops-runbook.md](docs/ops-runbook.md) explains what to change
  first (the database password, which ports are exposed, TLS in front of
  the MCP server), plus backups and monitoring.

Either way you need Postgres 16 or newer with pgvector 0.8 or newer, and
an embedding service the servers can reach.

Every release is published as a container image at
`ghcr.io/monochrome-muncher/hivemind`, tagged with the release (for
example `v2.5.0`). One image
runs every part; the `HIVEMIND_RUNNER` variable picks which (`api`,
`mcp-http`, `admin`, `portal`, `migrate` or `keys`). Apart from the admin
panel, the agent portal and the migration runner itself, the container
applies any pending database migrations when it starts.

Before you upgrade, read the release's row in the
[CHANGELOG](plugins/hivemind/CHANGELOG.md). Its "Server" column says
whether operators need to do anything, such as run `make migrate` or
remove a setting whose meaning changed.

## When something goes wrong

- **Saving an entry fails with `embedding_unavailable`, or searches only
  match exact words.** The embedding service is down or still loading.
  Check `docker compose logs vllm`, or your provider settings.
- **The REST API answers `missing_api_key`.** It wants the key in an
  `X-API-Key` header, not in `Authorization`.
- **`make mcp-http` or `make mcp-http-dev` fails with `address already
  in use`.** Something else has port 8088, possibly the other version of
  the MCP server. Stop it, or pick another port with
  `HIVEMIND_MCP_HTTP_PORT=9000`.
- **A setting in `.env.local` has no effect.** The same variable is
  probably exported in your shell, and that wins. Check with
  `env | grep HIVEMIND_`.
- **`make migrate` complains about the vector dimension.** The database
  was set up with a different `HIVEMIND_EMBEDDING_DIM`. Use the same
  value, or run `make pg-reset`, which deletes all data.
- **`Cannot connect to the Docker daemon`.** Docker is installed but not
  running. Start Docker Desktop or the Docker service.
- **`permission denied` when Docker starts (Linux).** Your user is not
  allowed to use Docker. Add yourself to the `docker` group with
  `sudo usermod -aG docker $USER`, then log out and back in.

## Documentation

- [plugins/hivemind/README.md](plugins/hivemind/README.md): connecting
  your agent.
- [DEPLOY.md](DEPLOY.md): running Hivemind on Kubernetes.
- [docs/ops-runbook.md](docs/ops-runbook.md): running it on one machine,
  backups and monitoring.
- [plugins/hivemind/CHANGELOG.md](plugins/hivemind/CHANGELOG.md): what
  changed in each release.
- [SPEC.md](SPEC.md): exactly what the system does.
- [CONTEXT.md](CONTEXT.md): what each term means.
- [docs/adr/](docs/adr/README.md): why each design decision was made.
- [ROADMAP.md](ROADMAP.md): what is planned and what is on hold.
- [AGENTS.md](AGENTS.md): working on the code (commands, layout, rules).

## Working on Hivemind

```bash
make test-unit   # unit tests, no database needed
make test        # all tests (see the warning below)
make check       # type checks and lint
make format      # fixes formatting
```

`make test` empties and drops tables in the database that
`HIVEMIND_DATABASE_URL` points to. Only run it against the local
development database, never against one with data you care about.

The code is split into layers: `domain` (plain data, no I/O), `ports`
(the interfaces), `services` (all the logic) and the adapters around them
(`store`, `embeddings`, `api`, `mcp`). [AGENTS.md](AGENTS.md) has the
full map and the checks a change has to pass. If you're contributing,
start with [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

Hivemind is released under the [MIT License](LICENSE). Use it, change
it, ship it in your own products, commercial or not. The only condition
is keeping the copyright notice in the LICENSE file with your copies.
