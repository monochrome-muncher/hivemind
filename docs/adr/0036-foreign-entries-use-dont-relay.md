# Foreign entries: use, don't relay — an agent rule, not a server rule

## Context

A privileged agent (level 3) reads every fleet and writes only into its
home fleet (ADR 0011, "read-broad, write-local"). "Write-local" controls
where an entry is filed, not what it says: nothing stopped a privileged
agent from reading another fleet's entry and restating it in its home
fleet. Fleets here are a mix: most partition knowledge by relevance, some
are sensitive, and an agent cannot tell which.

No server rule can prevent this. Once the model has read an entry, the
content is just text it may paraphrase.

## Decision

1. **A conservative agent rule** in the hivemind skill for **foreign
   entries** (filed outside the reader's home fleet): use them in your own
   work and answers; do not restate, summarise or copy them into a fleet
   entry; cite the id in `sources` instead (readers outside that fleet get
   `not_found`, so the link leaks nothing); ask the user before bringing a
   foreign finding home; your own `self` notes are fine.

2. **Make foreign entries recognisable**: search hits carry `scope` and
   `fleet_id` on both surfaces, so an agent compares `fleet_id` with its
   `home_fleet_id` (from `hive_whoami`) before opening an entry.

3. **Not now: a per-fleet "sensitive" flag.** An admin-set flag shown on
   hits, with the server rejecting fleet entries that cite a sensitive
   fleet's entries, is the next step if the conservative default proves
   too loose. It needs a migration, an admin-panel toggle and its own ADR.

## Consequences

* The real control over cross-fleet exposure is still **who is made
  privileged**: the rule relies on the agent following instructions.
* Hits grow two fields; clients that ignore unknown fields are unaffected.
