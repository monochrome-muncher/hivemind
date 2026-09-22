# Reject unknown `HIVEMIND_*` environment variables

`Settings` (`src/hivemind/config.py`) has always used
`model_config = SettingsConfigDict(env_prefix="HIVEMIND_", extra="ignore", ...)`.
`extra="ignore"` means a `HIVEMIND_*` variable that doesn't match a
`Settings` field is silently dropped — it does not raise, it does not
warn, it just does nothing. On a laptop that's an annoyance. On an
air-gapped production cluster, where the only feedback loop is "the
behavior didn't change, or changed in a way nobody can explain," it is a
long debugging session with no error message to search for.

It is not hypothetical: ADR 0021 renamed
`HIVEMIND_EMBEDDING_PREFIX_CHARS` to `HIVEMIND_EMBEDDING_PREFIX_TOKENS`.
Any deployment that still exports the old name today gets the *default*
prefix budget, silently, forever — `extra="ignore"` was the mechanism
that let a real, shipped rename become an invisible regression for
anyone who missed the changelog.

## Decision

`load_settings()` — the function every runner (`api/main.py`,
`mcp/http.py`, `mcp/server.py`, `store/migrate.py`, `store/keys.py`)
actually calls to build its `Settings` — now rejects any `HIVEMIND_*`
variable that is **neither a `Settings` field nor on an explicit
allowlist**, checked against both real process env vars and the active
`ENVIRONMENT` profile file (ADR 0017), before `Settings` is constructed.

**The allowlist is one dict, `_ALLOWED_EXTRA_ENV_VARS` in
`src/hivemind/config.py`, with a comment per entry saying why it's
exempt.** Five variables are deliberately not `Settings` fields, because
they are read straight out of `os.environ` by code that never
constructs `Settings` at all:

| Variable | Set by | Read by |
|---|---|---|
| `HIVEMIND_RUNNER` | Dockerfile `ENV` / `entrypoint.sh` / k8s ConfigMap | `entrypoint.sh` — selects the runner |
| `HIVEMIND_HOST` | Dockerfile `ENV HIVEMIND_HOST=0.0.0.0` | `api/main.py`, `mcp/http.py` via `os.environ` |
| `HIVEMIND_PORT` | k8s Deployment env | the runners via `os.environ` |
| `HIVEMIND_MCP_KEY` | per-agent operator config | `mcp/server.py` via `os.environ` |
| `HIVEMIND_MCP_HTTP_PORT` | `docker-compose.yaml` | compose only; the app never reads it |

A future reader can tell "deliberately not a setting" (in the table
above, with a reason) from "forgotten" (not in the table, and now a
loud startup failure instead of a silent no-op).

**The error names every offending variable** and, cheaply, suggests the
nearest valid name via `difflib.get_close_matches` against the combined
set of field names and allowlist entries — so the `PREFIX_CHARS` →
`PREFIX_TOKENS` rename now fails loudly and points at the new name,
instead of silently keeping the old default.

## Where the check lives, and why not on the model

The natural first instinct is `extra="forbid"` on `Settings` itself.
**That is wrong and was verified wrong before being ruled out**: three of
the five allowlisted variables (`HIVEMIND_RUNNER`, `HIVEMIND_HOST`,
`HIVEMIND_PORT`) are set on every pod (Dockerfile `ENV` / the k8s
ConfigMap), so a bare `extra="forbid"` would make *every* container
refuse to build `Settings` — a self-inflicted crash-loop, not a caught
typo. Pydantic's `extra` config is a global switch (forbid everything
extra, or ignore everything extra); it has no "forbid, except these
names" mode, so expressing the allowlist at the model level would mean
either abandoning `extra="ignore"`'s safety for legitimate non-field
vars, or hand-rolling the same allowlist logic inside a validator
anyway — at which point it belongs next to `load_settings`, not hidden
inside model validation that every direct `Settings(...)` construction
would also pay for.

**So the check lives in `load_settings()`, not on the model:**

- `load_settings()` is the real deployment entry point — every runner
  calls it, never a bare `Settings()`, to build its live configuration.
  It is also the one place that already knows the active profile file
  (`env_file_for`), so it can check that file's contents too, not just
  `os.environ`.
- `Settings` itself keeps `extra="ignore"`. Tests and dev code construct
  `Settings(...)` directly with explicit Python kwargs throughout the
  suite (`tests/unit/test_embeddings.py`, `test_extractor.py`,
  `test_api_endpoints.py`, `test_probe_endpoints.py`, the `test_config_env_file.py`
  fixtures, and others) — always with real field names, never with the
  five allowlisted names, since those aren't `Settings` fields at all.
  A model-level `extra="forbid"` would not break any of these calls
  today, but it would silently start rejecting the allowlisted
  operational vars the moment any of those tests' *ambient* environment
  (not kwargs) carried one — which `os.environ` does, routinely, under
  `make test` and in CI. Keeping the check out of the model means a bare
  `Settings()` used for its declared defaults (as most of the test suite
  does) is never affected by what else happens to be exported in the
  shell; only the code path that claims to be "the deployed
  configuration" enforces the deployed environment's hygiene.

## Proof

```
$ HIVEMIND_NONSENSE=1 uv run python -c "from hivemind.config import load_settings; load_settings()"
RuntimeError: Unknown HIVEMIND_* environment variable(s) — neither a Settings
field nor on the exemption list in src/hivemind/config.py
(_ALLOWED_EXTRA_ENV_VARS):
  HIVEMIND_NONSENSE
Fix the name, unset the variable, or (if it is genuinely read outside
Settings) add it to _ALLOWED_EXTRA_ENV_VARS with a reason.

$ HIVEMIND_EMBEDDING_PREFIX_CHARS=500 uv run python -c "from hivemind.config import load_settings; load_settings()"
RuntimeError: ...
  HIVEMIND_EMBEDDING_PREFIX_CHARS — did you mean HIVEMIND_EMBEDDING_PREFIX_TOKENS?
...

$ HIVEMIND_RUNNER=api HIVEMIND_HOST=0.0.0.0 HIVEMIND_PORT=8000 \
  HIVEMIND_DATABASE_URL=postgresql://hivemind:hivemind@localhost:5432/hivemind \
  HIVEMIND_EMBEDDING_ENDPOINT=http://localhost:8001/v1 \
  HIVEMIND_EMBEDDING_DIM=512 \
  uv run python -c "from hivemind.config import load_settings; print(load_settings().database_url)"
postgresql://hivemind:hivemind@localhost:5432/hivemind
```

(Exact transcripts, including the entrypoint/runner scenario, are in the
task report at `.superpowers/sdd/tier4-followups/task-bc-report.md`.)

## Interaction with ADR 0023

See ADR 0023's "Interaction" section: this check is about variable
*names*; ADR 0023 is about the value grammar of one already-known name
(`HIVEMIND_RECENCY_FLOOR`). `HIVEMIND_RECENCY_FLOOR=` passes this check
unmodified (it's a real field) and is then interpreted as `None` by
ADR 0023's validator. The two are complementary, not overlapping: the
environment should be able to say exactly what it means — a stale name
is rejected here, and a genuine "off" value is made expressible there.

## Consequences

- `entrypoint.sh` and both MCP runners are untouched — they read
  `HIVEMIND_RUNNER` / `HIVEMIND_HOST` / `HIVEMIND_PORT` / `HIVEMIND_MCP_KEY`
  exactly as before; this change only adds a check upstream of
  `Settings` construction inside `load_settings()`, in a different
  module.
- A future `HIVEMIND_*` variable that's meant to be read outside
  `Settings` must be added to `_ALLOWED_EXTRA_ENV_VARS` with a reason,
  or every deployment that sets it starts failing at startup. That is
  the intended friction — it is the same review gate a new `Settings`
  field already gets, applied to the one class of variable that used to
  skip it entirely.
- Bare `Settings()` (tests, ad hoc scripts) is unaffected: the check
  runs only in `load_settings()`.
