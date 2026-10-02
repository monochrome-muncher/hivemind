# Reads by id, write provenance and supersession are enforced by the server

> **Amended by [ADR 0040](0040-input-bounds-and-agent-name-rules.md):**
> the reserved names are compared case-insensitively (`casefold()`), and agent
> names must match a fixed ASCII format at registration.
>
> **Amended by [ADR 0060](0060-the-admin-key-writes-self-or-org-entries.md):**
> the admin key writes only `self` or `org`, and an admin successor must be
> `org` unless it replaces the admin's own `self` note.

## Context

A deployed contributor fetched another agent's `self` entry with
`hive_get` and got it back in full. An audit of every path that touches
an existing entry found the trust matrix (ADR 0011) applied to search and
list only:

1. **Reads by id ignored visibility.** MCP `hive_get` and REST
   `GET /v1/entries/{id}` returned any entry to any valid key, the org
   key and level 0 included. The supersession-chain walk behind
   `include_history` / `?history=true` did the same, so one visible entry
   could expose every version linked to it.
2. **MCP writes trusted a caller-supplied `author`.** `self` visibility is
   decided by `entry.author == reader`, so an agent passing
   `author: "X"` wrote an entry readable by whoever is registered as
   `X`. REST already stamped `author` from the key (ADR 0012: provenance
   is "server-verified, never self-reported"); MCP did not, and its tool
   description invited the argument. `agent` was self-reportable too.
3. **Supersession had no check at all.** Any write could flip any entry,
   by id, to `superseded` — including entries the writer could not see,
   and fleet entries hidden behind a successor only the writer could
   read (a lurker's `self` entry superseding a fleet entry hides it from
   the whole fleet).
4. **Feedback ignored visibility**, although SPEC §12.2 says feedback and
   withdrawal "follow readability".
5. **Built-in identity names were registrable.** An agent named `admin`
   would read `self` entries written with the admin key.

## Decision

1. **Every read by id applies the reader's visibility.** An entry the
   caller cannot see answers **not found** (`hive_get`,
   `GET /v1/entries/{id}`, `hive_feedback`, `hive_withdraw`) — never
   "forbidden", so an id reveals nothing, not even existence. The
   supersession-chain walk drops versions the caller cannot see, without
   a placeholder.

2. **Write provenance comes from the key on both surfaces.** MCP
   `hive_write` loses its `author` parameter; `author` is the key's
   registered name, as on REST. For an agent key the key's `agent` wins
   over a supplied one; the `agent` argument remains only for legacy v1
   keys that have no agent identity.

3. **Supersession stays a claim, within the audience the writer can
   address** (amends SPEC §4.1's "any user may supersede any entry"). A
   writer may supersede an entry only if it can read it **and** the
   successor reaches at least everyone the predecessor reached:

   | New entry's scope | May supersede |
   |---|---|
   | `self` | the writer's own `self` entries |
   | `fleet` | the writer's own `self` entries, and `fleet` entries of the fleet the new entry is written to |
   | (admin / legacy key) | anything |

   The legacy `org` scope is not writable by agents, so only an admin
   supersedes `org` entries. A write naming any target outside the rule
   is **rejected as a whole**; the error lists those ids as "not found or
   not supersedable by you", the same wording for both cases.

4. **Feedback follows readability** (SPEC §12.2): feedback on an entry
   the caller cannot read is "not found".

5. **`admin`, `org`, `dev` and `shared` are reserved agent names** — the
   identities built-in keys and runners write under. Registering one is a
   `name_conflict`.

## Consequences

* A lurker can no longer "correct" a fleet entry by superseding it from
  `self`. Its route is `hive_feedback` (`stale` / `wrong`, with a note)
  plus its own `self` entry; the agent skill says so.
* A privileged agent reads every fleet but supersedes only in its home
  fleet: read-broad, write-local (ADR 0011) now holds for supersession
  too.
* Clients that passed `author` to MCP `hive_write` get an
  unknown-argument error instead of a silently misattributed entry.
* Entries already written with a spoofed `author` are not rewritten: the
  true writer cannot be recovered (`agent` was spoofable too). The ops
  runbook lists them for an operator to review and withdraw.
