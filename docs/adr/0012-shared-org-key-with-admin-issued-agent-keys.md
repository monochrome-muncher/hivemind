# Shared org key with admin-issued agent keys

ADR 0008 issued per-*user* keys plus agent-scoped sub-keys: the human
author sat inside the credential, and the agent key bound an
(author, agent-instance) pair. That model assumed a human in the loop
for every write. The fleet/trust model (ADR 0011) moves identity onto
the **registered agent**: the agent registers itself, the admin issues
its key, and the key itself carries the privilege. **Decision: three
credential kinds — one shared **org key**, admin-issued **agent keys**,
and the single **admin key**.**

This ADR supersedes ADR 0008's key kinds (`user` / `agent` / `admin` →
`org` / `agent` / `admin`). ADR 0008's *property* — provenance is
verified server-side, never self-reported — is retained and now binds to
the agent's registered name. ADRs 0009/0010's per-agent and
per-request credential *mechanisms* are unchanged; the key they verify
is now the agent key.

## Decision

* **Org key**: one per cluster, shared by every agent (and any human
  frontend). It gates **nothing privilege-wise**: a request carrying
  only the org key may register (`hive_register`) and hit health — all
  data-plane privilege comes from the agent key. **Rotation is the
  cluster-wide kill switch**: `POST /v1/admin/org-key/rotate` (also
  `hivemind-keys rotate-org`) takes effect on the very next request.
* **Agent key**: issued by the admin at agent activation; one per
  agent; carries the agent's **trust level** and **home fleet**. The
  entry's `author` becomes the agent's **registered name, filled
  server-side from the key — never self-reported** (ADR 0008's
  verified-provenance property, now bound to the agent itself). The
  legacy `agent` (instance-id) column is retired from writes (kept in
  the schema for existing data).
* **Admin key**: the single credential for the admin surface — list
  agents/fleets, **activate** (set trust level + home fleet, **return
  the generated agent key once — the only moment a key is ever shown**
  to anyone), promote/demote, revoke, create fleets, rotate the org key.
  Admin-issued entries use the reserved name `admin`.

## Registration flow

1. An agent self-registers — via `hive_register` (MCP, org key only) or
   `POST /v1/agents` (REST; org key **or** admin key — the seam a
   future human-facing frontend plugs into) — with a **unique agent
   name** plus the **owner's alias** (a username or email the admin can
   use to reach the owner). This creates a **pending** agent at trust
   level 0 (untrusted): no data-plane access.
2. **Name already pending** → idempotent no-op ("registered — awaiting
   admin activation"). **Name already active** → "name already
   registered to an active agent — choose a new name." (A squatter can
   never double-register an active agent's name.)
3. The admin **activates**: sets the trust level (default 1, `lurker`)
   and the home fleet (required; fleets are created first via
   `POST /v1/admin/fleets`); the service generates the agent key and
   returns it **once**. The admin delivers the key **out-of-band**
   (chat/DM/email) using the owner alias — the service has **no
   notification channel**.
4. The **owner alias** lives on the `agents` record only (a contact
   field). It is **not** stamped on entries — entries carry the
   verified agent name; "who owns which agent" is a join, not a
   per-entry claim.

## Revocation, demotion, and names

* **Demotion to level 0** (`untrusted`) and **revocation** are
  **distinct verbs**: demotion keeps the key valid but the agent can do
  nothing (reads return empty, writes are denied); revocation kills the
  key (hard dead end — the agent re-registers under a new name or the
  admin re-activates the same name, which issues a *new* key).
* **Revocation does not free the name.** Agent names are durable
  identities (the same stance as ADR 0001's immutable entries):
  releasing names would invite a squatter to inherit a retired agent's
  self-scoped memory visibility. The owner simply registers a new name.

## Considered options

* **(a)** Keep ADR 0008's per-user keys; attach fleets/levels to the
  *user* instead of the agent — rejected: the human drops out of the
  credential in the new model, and per-agent privilege (not per-human)
  is what the fleet model gates.
* **(b)** Agents carry their own keys end-to-end (self-service key
  issuance, admin only approves) — rejected: a self-issued key would
  bind the agent to whatever it *claims* as its privilege; privilege
  must be admin-assigned to be meaningful.
* **(c) Shared org key + admin-issued agent keys — chosen.** One
  credential the whole cluster shares (cheap to rotate as a kill
  switch), one per-agent credential (per-agent revocation and
  attribution, as ADR 0009/0010 already require), one admin credential
  (the governance surface).

## Consequences

* The `credentials` table re-shapes: kinds `org` / `agent` / `admin`
  (still one row per key, hash-only storage — a leaked database never
  leaks usable keys, SPEC §8.1).
* `hive_register` is the **seventh** MCP verb; it is the only verb
  callable with the org key alone.
* `hivemind-keys` CLI is **reduced** to admin-key issuance +
  org-key rotation: per-agent key issuance moves to the admin surface
  (the CLI's agent-issuance path is removed, not deprecated).
* **Migration**: existing `user`/`agent`-kind rows must be re-issued as
  agent keys under registered agent names (a named migration step in the
  ROADMAP access-control track); until migration, legacy rows keep
  working with their old semantics.
* The dev runner (`hivemind-mcp`, hard-coded `dev` credential) is
  unchanged — it is a zero-dependency dev path, not part of the
  governance model.