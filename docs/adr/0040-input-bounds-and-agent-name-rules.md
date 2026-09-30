# Input bounds, NUL rejection and agent-name rules

## Context

A review of the REST and MCP surfaces found the same gap everywhere: the
service accepted whatever the wire carried and let Postgres be the
validator.

* **U+0000** is legal in JSON (`"\u0000"`) and in URLs (`%00`) but cannot
  be stored in a Postgres `text` or `jsonb` value. asyncpg raised, nothing
  mapped the error, and the client got a bare 500 (REST) or an opaque
  "Error executing tool" (MCP) — after the embedder had already been paid
  for. Reachable from every string a caller supplies.
* **Sizes** were unbounded except `summary`. A ~150k-distinct-word `body`
  overflows the 1 MB `tsvector` limit (`ProgramLimitExceeded`, a 500 after
  embedding and extraction); 50 000 tags, a 100 kB tag, empty tags, 1 MB
  feedback notes and a 200 MB request body were all accepted.
* **`limit` / `offset`**: `GET /v1/entries` had no ceiling (and an int64
  overflow was a 500); `POST /v1/search` validated nothing, so a negative
  value meant Python slice semantics.
* **Agent names** were any string. `Admin`, `admin` + zero-width space,
  `` (empty), a 100 000-character name and names containing `/`, `?`, `#`
  all registered; the name becomes the verified `author` of every entry
  the agent writes, so a lookalike of a reserved identity is impersonation,
  and a name that cannot be addressed in a URL path is an unactivatable,
  unrevocable ghost.
* The **admin panel proxy** rebuilt the upstream URL from the percent-
  *decoded* path, so `bob%23` was forwarded as `bob#` — a fragment — and
  mutated agent `bob`.

## Decision

**One shared validator module, `domain/validation.py`, used at the domain
and service seams, so REST and MCP refuse the same inputs, before any
embedder or store call.** Violations raise `InvalidInput` (a `ValueError`),
which REST answers **422** and MCP `invalid_input`. Where a surface maps
`ValueError` to something else (REST register: 409, MCP register:
`name_conflict`, withdraw: `not_active`, feedback: `agent_unresolved`) it
catches `InvalidInput` first.

Where each check lives:

* `EntryDraft` (construction) — NUL + caps on summary, body, tags,
  sources, payload (nested), supersedes, author/agent. The draft is built
  before `WriteService.write` embeds, so validation precedes the embedder.
* `EntryFilters` (construction) — NUL + caps on every filter string.
* `SearchService.search` — query NUL/length and `limit`/`offset` bounds,
  before the embedder is called.
* `GovernanceService` — feedback `note` / `agent`, withdraw `reason`.
* `AccessService.register` / `create_fleet` — names and owner alias.
* `get_visible_entry` — an id with a NUL cannot name an entry: not found.
* REST path/query parameters that name stored records (`{name}`, audit
  `actor`, `home_fleet_id`) — pydantic `max_length` + NUL rejection (422).

The bounds (constants in `domain/validation.py`; SPEC §4.1/§5.3 restate
them): `limit` 1–100, `offset` 0–10 000; `query` ≤ 2000 chars; `body` ≤
100 000 chars; `tags` ≤ 32, each non-blank ≤ 64; `sources` ≤ 32, `ref` ≤
2048; `payload` ≤ 64 KiB plain JSON (no NaN/Infinity); `supersedes` ≤ 16;
`note`/`reason` ≤ 2000. Caps are deliberately generous relative to real
use (the `summary` is ≤ 280; a distilled entry is not a document store)
and every one is a constant, so loosening one is a one-line change.

**REST request guard** (`api/guards.py`, pure ASGI middleware): a body over
2 MiB is refused 413 by `Content-Length` and by counting streamed bytes,
before FastAPI parses it; a `/v1` request with no `X-API-Key` header is
refused 401 before its body is read (the two public probes excepted).
The key is **not verified** in the middleware — that is a store round trip
the dependency already does — so a present-but-unknown key with a body
under the cap is parsed and then refused 401. That residual is bounded
(≤ 2 MiB) and is documented rather than worked around. No rate limiting
(still deferred, ADR 0029).

**Agent names** (`validate_agent_name`, called by `AccessService.register`
so both surfaces share it): ASCII `[A-Za-z0-9][A-Za-z0-9._-]{0,62}`.

* *Taken as given, never normalised.* NFKC + lowercasing would silently
  register `ａｄｍｉｎ` or `Bob` as a different string from the one the
  agent asked for, and the agent would then use the wrong name. Rejecting
  is honest and ASCII-only closes the lookalike / zero-width / control /
  whitespace class without a Unicode-category allow-list to maintain.
  (Normalising is still what the *reserved-name comparison* does:
  `name.casefold()`, so `Admin`/`ORG` are refused; nothing non-ASCII can
  reach it.)
* *Case is allowed*, so every name that was valid under the old rules and
  is plausible in practice (`alice`, `Alice`, `john-claude-infra`) stays
  valid. Case-variant collisions between two non-reserved names are not
  prevented; only the reserved identities (ADR 0033) need protecting.
* No leading `.`, `_`, `-`; this also makes `.` and `..` impossible, and
  the charset excludes `/`, `?`, `#`, `%` so every name is addressable as
  one URL path segment.
* Length ≤ 63; existing agents with older names are untouched — lookups,
  activation and revocation bound and NUL-check the name but do not apply
  the format, so a legacy agent stays manageable.

Fleet names are free-form display text: non-blank, ≤ 128 characters, and no
control (Cc), format/zero-width (Cf), separator (Zl/Zp) or NUL characters.
`owner_alias` likewise (≤ 128).

**Admin panel proxy** re-quotes each decoded path segment
(`quote(segment, safe="")`) when building the upstream URL and refuses
`.` / `..` segments (404 `not_proxied`), so a name is forwarded exactly as
the allowlist matched it.

**Amendment to ADR 0021** (not a contradiction of it): the embedded text
also carries a character ceiling (`EMBED_BODY_MAX_CHARS = 20 000`) beside
the 2000-word budget. The word budget stays the primary bound; the ceiling
covers bodies with no whitespace (base64, minified JSON, CJK), which count
as one "word" and previously reached the embedder whole.

**Documented, not changed:** a search ranks at most `2 × candidate_top_k`
entries (SPEC §5.3), so paging past that returns an empty page.

## Consequences

* Clients that sent `limit` > 100, an empty tag, a > 100 000-character body
  or a non-conforming agent name now get a 422. Existing stored rows are
  untouched; nothing is migrated.
* The bounds are in the `hive_search` / `hive_list` docstrings; the registered tool descriptions (and the plugin skills that mirror them) are unchanged.
* Unknown `/v1` paths with no key now answer 401 rather than 404.
* The MemoryStore still accepts NUL (it cannot fail); the service layer is
  what refuses it, and `tests/integration/test_input_hardening_pg.py` pins
  the behaviour against real Postgres.
* Ingress limits should sit below the 2 MiB app cap; this ADR does not
  change deployment manifests.
