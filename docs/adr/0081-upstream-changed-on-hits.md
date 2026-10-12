# A hit shows when an entry it links to changed

Adds to SPEC §6.1 (the evidence on a hit), built on the links of [ADR 0057](0057-see-also-links-between-entries.md) and the evidence fields of the PR #117 era ([ADR 0070](0070-one-capped-budget-for-social-signals.md) for the ranking budget). Does not change ranking, scoring or the order of any result.

Status: Accepted. Display only, on every search.

## Context

An insight or decision is usually built on other entries: the writer links them with `see_also` (ADR 0057). When one of those is later withdrawn, replaced by someone else's correction, or reported stale or wrong, the entry built on it is exactly as exposed as the thing it rests on, and nothing tells its reader. A retraction does not travel along the link. The study of 2026-10-10 (section 3.3 and plan row 14) approved a marker for this, one hop deep, for insights and decisions only. Feedback-derived signals never rank beyond the capped budget of ADR 0070, so the marker must not rank at all.

## Decision

1. **A field on the hit.** `upstream_changed`: a list of at most 3 `{id, change}`, where `change` is `withdrawn`, `superseded` or `flagged` and `id` is the linked entry. Over MCP the field is omitted when empty; over REST it is `null` (the other evidence fields behave the same, SPEC §6.1). It is on search hits only, which is where `contested` and `correction` are.

2. **Only insights and decisions.** A fact is an observation, not built on anything. The rule runs for hits of kind `insight` or `decision` and never for a `fact`.

3. **What counts as changed**, for each outgoing link of the hit (`entry_links.from_id` = the hit), judged against the link's `created_at`:
   - `withdrawn`: the target is withdrawn.
   - `superseded`: the target is superseded by an entry created strictly after the link whose author differs from the hit's author. A writer replacing its own link target is not news to itself.
   - `flagged`: the target's newest `stale` or `wrong` report is strictly newer than the link and no noted `helpful` report is newer than that flag. This is the "last confirmed" rule of SPEC §4.2 (`last_confirmed`, revoked reporters left out, ADR 0067), reused as is: a bare `helpful` does not clear a flag, a tie does not clear it.
   A target that is both is listed once, withdrawn before superseded before flagged.
   Limits of the supersession rule: the chain is not walked, only the target's direct successor counts; all admin keys write as author `admin`, so one admin superseding a target that another admin's entry links to is not reported; and the successor's visibility is not checked. That reveals only that the target was superseded by a different author after the link, no more than `state` and `superseded_by` on reads already reveal, and bounded by `may_supersede` (ADR 0033).

4. **Order and cap.** Withdrawn targets first, then newest change first, ties by target id; at most 3.

5. **Only targets the reader may read.** The visibility matrix (ADR 0011), ignoring state: a link whose target the reader cannot read is left out as if it did not exist, so the field never leaks an id (ADR 0033).

6. **One batched store read.** `Store.upstream_links(entry_ids, visibility=)` returns the raw facts per entry (the links, each target's state, its successor's author and creation time, its newest flag date and newest noted `helpful` date), implemented in `PgStore` (one statement) and `MemoryStore` and held to one contract. The domain function `upstream_changed` in `domain/upstream.py` applies the rule. The search service asks once per search, for the hits of the returned page that are insights or decisions, after the existing gathered reads (ADR 0059). It is best-effort like version grouping (ADR 0074): a failed read logs a warning and means no marker, never a failed search. The query reads `entry_links` by its primary key `(from_id, to_id)`, which serves `from_id`; an entry has at most 5 links (ADR 0057). No migration.

7. **Display only.** Nothing here touches scoring or order. A test compares the scores and order with the marker read and not read.

8. **Entries stay data.** The marker says that something changed, not what to do; the id points at an entry the reader opens like any other (ADR 0043). The tool description gets one clause and the always-on skill one line telling the agent to re-check such a hit before relying on it.

## Known limit: withdrawal has no date

Entries record no withdrawal time (the schema has no such column; only an admin withdrawing another agent's entry reaches the audit log, ADR 0027). So rule 3's "after the link" cannot be checked for `withdrawn`, and a withdrawn target always counts, even one that was already withdrawn when the link was made (a link may target an entry in any state, ADR 0057 decision 1). That is rare (the writer chose to link a withdrawn entry) and the marker is still true. Making it exact needs a `withdrawn_at` column, a migration the study did not approve; it is left as a follow-up.

## What it does not do

The "catch-up filter" half of plan row 14 (a way to list the entries whose upstream changed, as a feed for followers) is not in this change. It is a new query and a new parameter on two surfaces, and wants its own measurement of whether agents use the marker first.

Only one hop: a link to a link is not followed. `linked_from` (ADR 0057) is untouched. Reads by id do not carry the marker.

## Consequences

- A retraction reaches the entries built on it at the next search that returns them, without a query that happens to surface both.
- One more indexed read per search that returns an insight or decision, none otherwise.
- About 60 bytes of always-on agent text (one skill line, one description clause).
- A foreign entry the reader cannot read never appears in the field, so a link cannot be used to probe an unreadable entry.
