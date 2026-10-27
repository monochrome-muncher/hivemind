# Contributing

Thanks for your interest in Hivemind. This is a small project, and the
guide that goes with it is short too.

## Ground rules

- By contributing, you agree that your contributions are licensed
  under the [MIT License](LICENSE).
- Use short conventional-commit subjects: `feat:`, `fix:`, `docs:`,
  `refactor:`, optionally scoped (`refactor(mcp): …`).
- Be respectful. See the [Code of Conduct](CODE_OF_CONDUCT.md).
- For security issues, see [SECURITY.md](SECURITY.md): report them
  privately, not as a public issue.

## Getting started

You need Python 3.14 via [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make install   # creates .venv
make pg        # Postgres + pgvector in docker, on localhost:5432
make migrate   # applies the migrations to the dev database
```

Optional: `make vllm` starts a local embedding server. You only need
it if you're working on the embedding code.

## The loop

Every change has to pass:

```bash
make check      # mypy (strict) + ruff
make test-unit  # hermetic unit tests, no Postgres needed
```

`make test` runs the full suite, including the Postgres-backed
integration tests, in parallel (pytest-xdist: one worker per CPU, each
on its own database `<name>_gw0`, `<name>_gw1`, ..., created next to
the one `HIVEMIND_DATABASE_URL` names and kept for the next run;
`make pg-reset` or `DROP DATABASE` removes them). It empties the
tables in those databases, so only run it against the local dev
database. `make format` fixes most style complaints for you.

## Opening a pull request

- Small fixes (under ~30 lines) can go straight to a PR. Anything
  larger: open an issue first, so there's a discussion before the
  code.
- Branch from `main`, keep the change focused, and open a PR against
  `main`.
- Expect at least one round of review.

## Before you go deep

- [AGENTS.md](AGENTS.md): the full map of the codebase and the
  invariants a change has to respect
- [SPEC.md](SPEC.md): what the system is supposed to do
- [docs/adr/](docs/adr/): the decisions, and why they were made
