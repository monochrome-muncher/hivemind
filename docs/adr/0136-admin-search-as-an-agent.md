# The admin can search as an agent

Extends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin search and the Memories view), builds on
[ADR 0011](0011-fleets-and-trust-levels.md) (the visibility matrix), [ADR 0113](0113-retire-an-agent-with-a-successor.md)
(inherited owners) and [ADR 0056](0056-count-searches-that-find-nothing.md) (admin searches are not counted).

Status: Accepted.

## Context

The most common support question is "why can't my agent find X?". The admin search sees every memory
in every fleet, so it finds X and answers nothing: the admin cannot tell whether the agent's trust
level, its home fleet, a private note of another author, a withdrawn memory or plain ranking is the
reason. The only way to reproduce the agent's view was its own key, which an admin should not hold
and the portal's single-agent sign-in cannot replace.

## Decision

1. **`POST /v1/admin/search` takes an optional `as_agent`.** When set, the search runs exactly as that
   agent's own `POST /v1/search` would: the same visibility (trust level, home fleet, its own `self`
   notes, the owners that hand down to it, the own-author widening) and the same ranking, so the ids and
   their order are the agent's. The `fleet_id` filter still applies on top. The route is still admin key
   only, still not counted in the search counters, and still audits nothing, since it is a read and
   admin reads are not audited.

2. **The credential is built through the `Authenticator` port.** `credential_for_agent(name)` returns
   what `verify` returns for that agent's key, built from the agent's record instead of a key, with
   `key_id` empty. `PgAuthenticator` selects the same columns and the same successor walk as the key
   check and maps them with the same function, and the in-memory authenticator builds it from the
   store, so the two cannot drift. A contract test holds the result equal to the verified credential
   minus its fingerprint on both adapters. No key material is read, returned or logged, and a key lock
   does not matter: it stops the key, not the agent's reach.

3. **Refusals.** An unknown agent is 404 `unknown_agent`. An agent that is not active (pending or
   revoked) is 409 `agent_not_active`, because such an agent cannot search. Both are typed service
   errors (`UnknownSearchAgent`, `AgentNotActive`, subclasses of `UnknownAgent` and
   `InvalidAgentStatus`) in the REST error table.

4. **The Memories view has an "As agent" box.** It is kept in the hash (`as=<name>`) and used only for a
   text search; with an empty query or a memory id the view says it does not apply. With an agent
   named, the view runs the agent's search and the admin's own with the same filters. The agent's
   results are the main table under the banner "Results as <agent> sees them (<trust>, home fleet
   <fleet>)" (the CSV export keeps working on it). A card "Hidden from <agent>" lists the admin's
   hits the agent did not get, each with a reason worked out in the browser from the agent's record and
   the hit: a private note of another author, in a fleet that is not its home fleet (below privileged),
   withdrawn or superseded, an untrusted agent that reads nothing, or otherwise ranked below the
   cut-off or filtered.

## Consequences

- An operator answers "why can't my agent find X?" in one screen, without the agent's key.
- The hidden card compares the first page of each search. A memory the agent could see but that ranks
  beyond its first page is reported as "ranked below the cut-off or filtered", which is true.
- The reasons are a diagnosis from visible facts, not a trace of the query. The rule they mirror is the
  visibility matrix; a change there changes the reason function in `asagent.js` too.
- The `Authenticator` port gains a method. Any new adapter implements it beside `verify`.
- `as_agent` is on the admin request only; the portal search request has no such field.
- The in-memory `FakeAuthenticator` now verifies keys it issued as the real, access-controlled credential
  (trust level, home fleet, inherited owners from the store), not a bare name, so unit tests match
  what `PgAuthenticator` returns.
- Nothing is stored and no migration is needed.
