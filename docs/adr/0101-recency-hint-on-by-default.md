# The recency hint is on by default

Amends [ADR 0073](0073-a-recency-hint-on-search.md) (decision 2, "behind a setting that is off by default"). The setting, `HIVEMIND_RECENCY_HINT`, stays; only its default changes.

Status: Accepted. On by default; set `HIVEMIND_RECENCY_HINT=false` to turn it off.

## Context

ADR 0073 gave a search an optional `recency` hint and kept it off until it had been measured with real embedders. Off, the server accepts the argument and ignores it. But the `hive_search` description offers the argument to every agent, so on a default server an agent that follows the description sends a hint that does nothing. The reply says so only by leaving out its `recency` key, which an agent rarely checks.

ADR 0073 recorded a first real-embedder run (2026-10-11) that supported turning the hint on for `ignore_age`, and left the default off. This re-run, with the query instruction now on (ADR 0097), is the one that settles it.

## Measurement

2026-10-21, `make measure-confirmation`. Qwen3-Embedding 8B Q8_0, 4B f16 and 0.6B f16 via llama.cpp, dim 1024, each embedder reported alone. The five-domain fixture of `tests/eval/confirmation.py`: 160 entries aged 0 to 400 days, 96 answerable queries, the hint applied to every query. Most of these queries are timeless, so this is the case where `ignore_age` should help and `prefer_recent` should hurt.

Pooled MRR, no hint, then `ignore_age`, then `prefer_recent`:

| embedder | no hint | `ignore_age` | `prefer_recent` |
|---|---|---|---|
| 8B | 0.729 | 0.738 | 0.724 |
| 4B | 0.741 | 0.745 | 0.687 |
| 0.6B | 0.711 | 0.743 | 0.653 |

Pooled hit@5 never drops with `ignore_age`: 8B 0.906 to 0.906, 4B 0.896 to 0.896, 0.6B 0.906 to 0.927.

Change in MRR with `ignore_age`, per domain:

| domain | 8B | 4B | 0.6B |
|---|---|---|---|
| data analysis | +0.121 | +0.132 | +0.066 |
| software engineering | +0.092 | +0.079 | +0.018 |
| system administration | +0.079 | +0.070 | +0.082 |
| journalism and social science | +0.027 | -0.029 | +0.027 |
| finance analysis | +0.088 | +0.053 | +0.026 |

`ignore_age` gains in 14 of 15 cells and never costs hit@5. `prefer_recent` lowers MRR in every domain, as expected on mostly timeless queries. It stays for current-state questions, for which there is no labelled real-embedder fixture yet; the age-varied fixture of ADR 0073 (hash embedder) is machinery only.

## Decision

1. **`SearchConfig.recency_hint_enabled` and `Settings.recency_hint` default to on.** `HIVEMIND_RECENCY_HINT=false` restores ADR 0073's off behaviour exactly: the argument is accepted, every search scores as `default`, bit for bit.

2. **A search with no hint ranks as before.** The hint acts only when an agent sends it, so no existing search changes its order because of this decision. The invariants of ADR 0073 are unchanged: I1 holds under `default` and `prefer_recent`, and does not hold under `ignore_age`, which is now reachable on a default server by an explicit hint.

3. **The tool description steers the choice.** `hive_search` tells agents to send `ignore_age` when the answer does not age and `prefer_recent` only for a question about what is current. The measurement is why: the cost of a wrong `ignore_age` is small on these queries, and the cost of an overused `prefer_recent` is not. The always-on budget is unchanged (18,000 bytes; the description change is paid for by shortening other wording in the same description).

4. **The eval stays comparable.** The hash-embedder gate and the sweeps send no hint, so they measure the same ranking as before. `tests/eval/confirmation.py` sets the hint off in its baseline and turns it on in its two hint variants, so each variant still measures what its name says.

## Consequences

- A default server now honours `recency`. Deployments that want the old behaviour set `HIVEMIND_RECENCY_HINT=false`; `config/.env.example`, the Kubernetes configmap and the ops runbook say so.
- A mistaken `ignore_age` can put an old, wrong entry above a newer correct one, as ADR 0073 says. Hits carry `occurred_at`, `feedback` counts and `stale` reports, so the agent can see it.
- The reply still says which hint was applied (`X-Hivemind-Recency`, `"recency"` in `hive_search`), so an agent can tell a server that has the hint off.
- `prefer_recent` is still unproven for current-state questions. A time-labelled real-embedder fixture would settle its floor (0.5, ADR 0073); until then it is advice for "what is current" questions only.
