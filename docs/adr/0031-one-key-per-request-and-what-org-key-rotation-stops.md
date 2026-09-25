# One key per request; rotating the org key closes registration, nothing more

Supersedes the "kill switch" clause of ADR 0012.

## Context

ADR 0012 called org-key rotation "the cluster-wide kill switch", and
SPEC §12.3 said an active agent "presents the org key + its agent key
on every request". The code never did either. Both surfaces
authenticate **exactly one key** per request (`X-API-Key`, or
`Authorization: Bearer` on the MCP runner), an agent key is verified
on its own, and nothing checks for an org key alongside it. So
rotating the org key has only ever stopped new registrations; every
active agent kept working.

The docs also overloaded a glossary term: **kill switch** already means
the client-side act of turning Hivemind off for a session (ADR 0003).

Found while designing the agent plugin, which needs a clear answer to
"which key does my harness send?".

## Decision

1. **The code is right; the docs change.** A request carries one key.
   An agent sends only its agent key. Before activation it sends the
   org key, which lets it register and nothing else. Requiring both
   keys would give every agent two secrets to manage for no added
   control, since revoking an agent already cuts it off.

2. **Org-key rotation closes registration.** It stops every prior org
   key at once, so nobody can register new agents with a leaked or
   departed copy. Active agents are unaffected. The ways to cut off
   agents are revoking them one at a time (ADR 0028) or demoting them
   to `untrusted`.

3. **"Kill switch" means only ADR 0003's client-side switch.** Org-key
   rotation is described as *rotating the org key* or *closing
   registration*, never as a kill switch.

## Consequences

* Stopping every agent at once has no single verb. If that is ever
  needed it is a new decision (for example, a cluster-wide read-only
  flag), not a reinterpretation of org-key rotation.
* An agent's harness configuration holds one secret,
  `HIVEMIND_API_KEY`: the org key until activation, then the agent key.
