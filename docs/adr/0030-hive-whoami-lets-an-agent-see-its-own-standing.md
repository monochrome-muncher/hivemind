# `hive_whoami`: an agent can read its own standing

## Context

An agent could not find out what it is allowed to do. Its trust level
and home fleet live on its agent record, but no verb returned them, so
it learned about missing rights only by trial:

* a lurker learned it cannot write `fleet` only when a write failed;
* an agent at level 0 (pending or demoted) could not tell "nothing
  relevant is stored" from "I may see nothing", because reads at level
  0 return empty results rather than errors (SPEC §12.2);
* an agent holding only the org key had no signal that it still needed
  to register and wait for activation.

The Hivemind agent skill (plugins/hivemind) asks the agent to tell its
user when it lacks the rights to contribute, and to fall back to
recall when it can read but not write. Both need the agent to know
where it stands before it acts.

## Decision

1. **An eighth MCP verb, `hive_whoami`, and `GET /v1/whoami`.** Both
   take no arguments and return the calling key's standing:

   | Field | Meaning |
   |---|---|
   | `key_kind` | `agent`, `org`, `admin`, or `legacy` (a v1 / dev-mode credential with no access control) |
   | `name` | the agent's registered name (agent keys), else the key's user id |
   | `status` | `pending` / `active` / `revoked` for an agent with a record, else `null` |
   | `trust_level`, `trust_level_name` | 0–3 and its name (`untrusted` … `privileged`) |
   | `home_fleet_id`, `home_fleet_name` | the agent's home fleet, or `null` |
   | `can_read` | what the key may read: `own`, `home_fleet`, `all_fleets`, `org` (the legacy scope), or `everything` (admin / legacy); empty at level 0 |
   | `can_write_scopes` | the scopes a write may use: `[]` at level 0 and for the org key, `["self"]` for a lurker (or a contributor with no home fleet), `["self", "fleet"]` for a contributor or privileged agent, `["self", "fleet", "org"]` for admin / legacy |

2. **Plain data, no advice.** The response says what the key can do,
   not what to do about it. Turning "`can_write_scopes` is `["self"]`"
   into "tell your user to ask for contributor" is the skill's job, and
   wording that changes should not need an API release.

3. **Any valid key may call it**, the org key included: an org-key
   caller learns it is `org` with nothing to read or write, which is
   exactly the signal to register. An unknown key is a 401, as
   everywhere else. It is a read, so it is **not audited** (ADR 0027).

4. **Derived from the same rules the data plane enforces.**
   `can_write_scopes` comes from `Credential.max_write_scope` and the
   home-fleet requirement in `resolve_write_scope`; `can_read` mirrors
   `entry_is_visible`. There is no second copy of the trust matrix to
   drift.

## Consequences

* SPEC §5.2's "seven verbs" becomes eight. `hive_whoami` is the only
  verb that is useful before an agent has any data-plane access.
* The key's trust level and home fleet are resolved when the key is
  verified, the same snapshot every other verb in that request uses; a
  promotion takes effect on the agent's next request.
