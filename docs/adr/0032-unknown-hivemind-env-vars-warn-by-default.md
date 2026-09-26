# Unknown `HIVEMIND_*` variables warn by default; failing is opt-in

Supersedes the "fail at startup" clause of ADR 0024.

## Context

ADR 0024 made `load_settings()` raise on any `HIVEMIND_*` variable that
is neither a `Settings` field nor on its short exemption list, so a
typo'd or renamed variable failed loudly instead of silently doing
nothing.

The first real deployment broke on it. GitLab Auto DevOps injects
variables of its own into the pod, and some of them share the
`HIVEMIND_` prefix. Kubernetes adds more on its own: every Service in
the namespace gets `<NAME>_SERVICE_HOST`, `<NAME>_SERVICE_PORT` and
`<NAME>_PORT…` variables, so Services named `hivemind-api`,
`hivemind-mcp` and `hivemind-admin` produce `HIVEMIND_API_SERVICE_HOST`,
`HIVEMIND_MCP_PORT` and so on in every pod. None of these are ours to
remove, and their exact names depend on the platform's release and
Service naming. An exemption list cannot keep up, and a guard that
crash-loops every pod over variables it does not own is worse than the
typo it catches.

## Decision

1. **By default, unknown `HIVEMIND_*` variables are logged, not fatal.**
   `load_settings()` still finds them, in the environment and in the
   active profile file, and logs one WARNING that names each one with
   the same "did you mean …?" hint. Startup continues.

2. **`HIVEMIND_STRICT_ENV=true` restores the hard failure.** It is a
   `Settings` field (so it is itself a known variable), read before the
   check runs. CI and local development can set it to keep ADR 0024's
   protection where the environment is fully under our control. It
   accepts `true`/`1`/`yes`/`on`, case-insensitive; anything else,
   including unset, means warn.

3. **The exemption list stays.** Variables Hivemind itself reads outside
   `Settings` (`HIVEMIND_RUNNER`, `HOST`, `PORT`, `MCP_KEY`,
   `MCP_HTTP_PORT`) are still known and never warned about, so the
   warning stays a signal rather than noise on every pod.

## Consequences

* A typo in production is visible only in the startup log, not as a
  failed rollout. That is the accepted cost of running on a platform
  that injects variables we do not control.
* Kubernetes service links can collide with a real setting. A Service
  named exactly `hivemind` would inject `HIVEMIND_PORT=tcp://…`, which
  the runners read as their bind port. The shipped manifests set
  `HIVEMIND_PORT` explicitly, and an explicit container variable wins
  over a service link. Setting `enableServiceLinks: false` on the pod
  removes the whole class of problem where the platform allows it.
