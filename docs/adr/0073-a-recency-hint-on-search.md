# A recency hint on search

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md), [ADR 0070](0070-one-capped-budget-for-social-signals.md) (invariant I1 does not hold under `ignore_age`) and [ADR 0072](0072-recency-profile-per-kind.md) (decision 4, "no decay is not offered": it stays unavailable as a setting, and exists only per search through the explicit hint), and SPEC §5 (search surfaces) and §6.4.

## Context

One recency curve scores every search: a 30-day half-life under a 0.8 floor
(ADR 0022), per kind if a deployment sets a profile (ADR 0072). The measured
cost is on both sides. "What is the current pool size?" wants the newer of two
near-identical entries; "how do we merge the two streams?" wants the best
match, however old. The server sees the query text and cannot tell the two
apart: "current" and "latest" are only wording, and the age-varied fixture has
a temporal-worded query whose right answer is a 380-day-old entry
(`tests/eval/temporal.py`, T1). The agent that asks knows whether its question
is about now. So the agent says so, per search, as a hint; the server does not
guess.

## Decision

1. **A `recency` hint on search**, one of `default`, `prefer_recent`,
   `ignore_age`; absent means `default`. REST `POST /v1/search` takes a
   `recency` field and MCP `hive_search` a `recency` argument. An unknown
   value is `422` / `invalid_input` on both, whether or not the setting below
   is on.

2. **Behind a setting that is off by default**:
   `SearchConfig.recency_hint_enabled` / `HIVEMIND_RECENCY_HINT`. Off, the
   parameter is accepted and ignored: every search scores as `default`, bit
   for bit, whatever a caller sends. It is accepted (not refused) so an agent
   or plugin that sends it works against any server, and the reply says which
   hint was applied so the agent can tell: REST adds the response header
   `X-Hivemind-Recency: <hint>` and `hive_search` adds `"recency": "<hint>"`,
   each only when a non-default hint was applied. Without it, the default
   scoring was used. This mirrors `X-Hivemind-Degraded` (ADR 0048).

3. **What each hint scores.** With `r` the recency factor of ADR 0022:
   - `default`: today's scoring, unchanged.
   - `ignore_age`: `r = 1.0` for every candidate, and no confirmation clock
     (ADR 0071; it is age again). Match, importance and the social factor
     (capped, ADR 0070) remain.
   - `prefer_recent`: `r = max(0.5, 0.5 ** (age / half_life))` with the
     entry kind's half-life (ADR 0072 still applies), and the confirmation
     clock as configured. The floor is `min(0.5, recency_floor)`, so it is
     never weaker than the deployment's own, and `none` stays `none`.

4. **Why a 0.5 floor for `prefer_recent`, measured.** On the age-varied
   fixture (hash embedder, machinery only; `docs/retrieval-experiments.md`)
   no floor gives one currency pair a rank (MRR 0.778 to 0.833) and buries
   exact matches everywhere else: non-temporal MRR 0.681 to 0.208, hit@5 for
   all queries 1.000 to 0.500. A floor of 0.5 bounds the term to 2x, inside
   the fused RRF range (2.62x), so match quality stays the sort key, and costs
   less (non-temporal 0.569, no slice improves). Any floor of 0.7 or more
   changes nothing on this fixture. Neither value improves the temporal
   slice, so `prefer_recent` is unproven; 0.5 is chosen because a hint is
   advisory, and a wrong or overused hint should cost a bounded amount. The
   real-embedder run decides whether it stays.

5. **The hint is an argument of one call**, never state of the service or the
   config, so one caller's hint cannot reach another's search. Candidates and
   fusion do not depend on it, only the rescore does; for pagination a client
   sends the same hint on every page, and the order is then one order.

## Invariants

- **I1 holds under `default` and `prefer_recent`, and whenever the setting is
  off.** A new unrated entry under about 1.7 days (per kind, with a profile)
  is never outranked by an old one at equal match and importance. `prefer_recent`
  only lowers the floor: an old entry's recency is at most `0.5 ** (10 / 30)`
  = 0.794 at 10 days and falls to the 0.5 floor, so its product with the 1.2
  cap stays below the 0.96 a new entry (under about 1.7 days) has.
- **I1 does not hold under `ignore_age`, by construction.** The caller asked
  that age not count, so a well-rated old entry can outrank a new unrated one.
  This is reachable only with the setting on **and** the explicit hint, which
  `tests/unit/test_recency_hint.py` pins in every other combination. It cannot
  be reached by omission, by `default`, or on a server that left the setting
  off. The rule "a new answer beats old applause" is the default's; this hint
  is the caller opting out for one search.
- I2: no new input reaches the score; the hint only chooses the half-life,
  floor and whether the confirmation date is used. I3, I4: the confirmation
  clock behaves as in ADR 0071 under `prefer_recent` and does not exist under
  `ignore_age`. I5: the social cap holds under every hint.
- Supersession (SPEC §6.3) is unaffected: within a chain the newest entry
  still ranks first, under every hint.

## Consequences

- Nothing changes until a deployment sets `HIVEMIND_RECENCY_HINT` and an agent
  sends a hint. The always-on tool text gains one short clause; the skill
  can say when to use it.
- A mistaken `ignore_age` can surface an old, wrong entry above a newer
  correct one. Entries carry `occurred_at` and `feedback` counts in the hits,
  and `stale` reports still lower quality, so the agent sees them; the cost
  is the agent's to bear.
- Real-embedder result (2026-10-11, 70/20/10, hint applied to every query,
  mostly timeless queries): `ignore_age` improves MRR in every domain (+0.009
  to +0.107) and pooled (+0.015, 0.737 to 0.752), and hit@5 never drops;
  `prefer_recent` lowers MRR everywhere, as expected for timeless questions,
  and stays unproven for current-state questions (no labelled real-embedder
  fixture yet). Because a hint acts only when an agent sends it, turning
  `HIVEMIND_RECENCY_HINT` on is low-risk, and the measurement supports
  enabling it for `ignore_age`. The default stays off.
- `make measure-ranking` reports `ignore_age` and `prefer_recent` applied to
  every query over the domain fixtures; those fixtures do not label time
  sense, so the labelled oracle comparison is the age-varied fixture's
  (`tests/eval/test_recency_hint.py`). Real-embedder numbers decide whether a
  deployment turns the setting on, and whether `prefer_recent` stays.
