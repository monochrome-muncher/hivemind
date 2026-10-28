# An agent sees when its own key was last used

Amends [ADR 0110](0110-agent-key-last-use.md) (last use was admin-only), [ADR 0030](0030-hive-whoami-lets-an-agent-see-its-own-standing.md) (`whoami` gains `key_last_used_at`) and [ADR 0095](0095-agent-portal.md) (the portal home gains a status card), and SPEC §5.1 and §5.2.

Status: Accepted. Design from the 2026-10-28 session with Monochrome.

## Context

ADR 0110 records when each agent key last authenticated, but only the admin panel showed it. The person behind an agent had no way to notice that the key had been used when the agent was idle, which is the sign that someone else holds it. The portal home also opened on a list of memories, with nothing about who the key is.

## Decision

1. **`GET /v1/whoami` and `hive_whoami` gain `key_last_used_at`**: an ISO 8601 string, the same value the admin list shows as `last_used_at`. It is `null` for an agent key not used since the column shipped, and for every other kind of key (admin, org, legacy). The field is always present, so clients need no special case.
2. **A new `Store.agent_key_last_used(name)`** returns that value for one agent (`None` if the agent has no key or it was never used). `list_agents` is not reused: it reads every agent, and `get_agent` stays free of key facts, as ADR 0110 left it. `PgStore` reads `credentials.last_used_at` by the agent's name; `MemoryStore` returns what its authenticator fake stamped. No migration.
3. **Any agent key sees its own value.** It reveals nothing the agent could not already learn about itself, and `whoami` reads only the caller's own row. Nothing ranks or permits on it (ADR 0110, point 8, still holds).
4. **The portal home shows a status card** above the memory list: agent name, status, trust level, home fleet, the agents it inherited from (ADR 0113) when there are any, and when its key was last used. The stamp is written at most once an hour, so the card says "Within the last hour" for a stamp under 60 minutes old, then a relative time, and shows the exact time with a note that it is recorded at most hourly. It also says, in one sentence, what to do if the time looks wrong: someone else may hold the key, so ask the admin to replace it (ADR 0108). A key never recorded shows "Not recorded". The card has no route of its own; it reads the `whoami` answer the portal already fetches on every page load and on Refresh.
5. **A fleet merge needs no line.** Merging moves the agents (ADR 0107), so `home_fleet_name` already names the fleet the agent is in now.

## Consequences

- `WhoamiOut`, `Standing.as_dict`, SPEC §5.1 and §5.2 and the plugin README list the new field. `tests/unit/test_agent_plugin.py` still pins that the docs name only fields `whoami` returns.
- Agents may mention an odd last-use time to the person who runs them. The plugin skill does not tell them to.
- The value lags by up to an hour, and an agent that has not authenticated since the migration shows "Not recorded" until its next request.
- One extra indexed read per `whoami` call for an agent key; `whoami` is not on the hot path.

## Considered options

- **Fill `last_used_at` on `get_agent`.** One query for both, but every caller of `get_agent` would pay a join for a value it ignores, and ADR 0110 scoped the fields to the list.
- **Add the time to the portal only (a portal route).** A second route for a value `whoami` can carry; agents would still not see it.
- **A "this looks wrong" button that flags the key.** More surface and an admin queue for a case a person can handle by messaging the admin.
