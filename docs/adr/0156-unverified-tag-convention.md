# Agents tag untested claims `unverified`

Builds on [ADR 0146](0146-scope-by-evidence.md) (scope by evidence), [ADR 0103](0103-remove-confirmation-clock-and-per-kind-recency.md) (`last_confirmed_at` is shown, never scored) and [ADR 0142](0142-vital-signs.md) (vital signs).

Status: Accepted.

## Context

ADR 0146 keeps untested ideas in `self`. A different case is a claim about how things are that the writer did not see hold: a likely cause found from a query plan but not tried, something the user relayed from another team, a fix that is deployed but not yet confirmed. Such a claim is worth sharing, and a reader should know it is untested. The 2026-11-12 assessment of how agents treat memories as data (option F) proposed marking it.

A structured confidence field was rejected: agents tend to set such a field to "high" by habit, and it would change the entry model, the contract and possibly ranking.

## Decision

1. A tag convention, not a field. `hive_write`'s description says "Tag a guess or a claim you did not test 'unverified'." The skill says when to use it and how it ends.
2. Search is unchanged. Hits already show `tags` and `last_confirmed_at`, so a reader sees the tag and whether someone has checked the entry since. Ranking does not read the tag (ranking invariants hold).
3. An entry is **still unverified** while it is active, carries the tag and has no `last_confirmed_at`. `domain.feedback.still_unverified` is the one rule; the Postgres twin is `sql_common.unverified_condition`. It ends in either of two ways, with no new state:
   - A confirming `helpful` report with a note from another agent sets `last_confirmed_at` (authors cannot rate their own entries). A later `stale` or `wrong` report clears that date, so the entry counts as unverified again.
   - A successor that supersedes it without the tag. The superseded entry is no longer active.
4. The admin memory browser and the portal show an "unverified" badge on such rows and offer an "Unverified only" filter (`unverified=true` on `GET /v1/entries`, `/v1/admin/entries` and `/v1/portal/entries`; not on search and not on `hive_list`). List rows carry `last_confirmed_at` in their `feedback` object for the badge.
5. Vital signs count still-unverified entries per fleet (`health.by_fleet.<fleet>.unverified`, Prometheus `hivemind_health_fleet_unverified`). It is shown in the breathing rows and never switches a fleet to Watch.

The always-on text measures 17,394 bytes without ADR 0155 and 17,429 with it.

## Considered and not done

- **A `confidence` field.** See Context.
- **A write warning that suggests the tag**, like `tentative` (ADR 0147). Left out until the live checks show weak models skipping the tag.
- **Clearing the tag in place.** Entries are immutable; the computed rule above needs no new state.
- **The filter on `hive_list`.** It would cost always-on bytes for something agents can do with `tags: ["unverified"]`.

## Consequences

- Fleets can see how much of what they share is untested, and readers can find the claims worth checking.
- An entry tagged `alternative` (an untested option) is often tagged `unverified` too, and counts.
- A plain `tags=unverified` filter still lists confirmed entries; the badge and the `unverified` filter apply the rule above.
