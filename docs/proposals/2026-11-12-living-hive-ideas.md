# A hive that lives and breathes: new feature ideas

*Proposal, 2026-11-12. Checked against `main` at 2591b6a (2.24.0). Ideas only: nothing here is approved, and nothing in the code was changed.*

## The question

Monochrome asked for a fresh brainstorm of Hivemind features drawn from hive minds and collective consciousness, aimed at three things: members exchange knowledge that is true and useful; the hive keeps a balance of exploration and exploitation; and it neither collapses into one view nor scatters into noise, but develops on its own over time.

This builds on the hive-mind study (`docs/proposals/2026-10-10-hive-mind-ideas.md`, including §8 "Sharing without converging"), the discovery ideas (`docs/proposals/2026-10-20-discovery-and-learning-ideas.md`), the 2026-10-01 knowledge-sharing and Caura notes, and the two feedback reviews (2026-10-10 and 2026-10-18). It does not repeat their ideas, and it re-proposes nothing that was dropped or rejected.

## The short answer

Hivemind already has a good **memory**: attributed entries, history kept, capped social signals, evidence on hits, rivals that stay alive, and convergence metrics for the admin. What it does not have yet is a **metabolism**: nothing in the system changes how the hive behaves when it starts to collapse or scatter. Every agent is told the same thing every day, so the balance between exploring and exploiting is fixed in the skill text and depends on each agent's judgement.

Living collectives handle this with three things Hivemind lacks:

1. **Roles.** A bee colony keeps a small, steady share of scouts who look for new options while most bees work the known ones (Seeley). March (1991) found the same in organizations: a code learns only from members who deviate.
2. **Rhythm.** Colonies, brains and good design teams alternate between spreading out and pulling together: foraging and swarm decisions, wake and sleep, the "double diamond". Simulated annealing (Kirkpatrick et al. 1983) is the same idea in maths: explore hot, then cool down.
3. **Feedback on the whole.** Homeostasis (Cannon 1932; Ashby's law of requisite variety) needs a sensor and a lever. Hivemind has the sensors (echo rate, topic dispersion, rival lifetime, grounding share), but they lead to no lever.

So my top three are about roles, rhythm and the lever, and none of them touches ranking or the always-on text:

| Rank | Idea | Hive concept | What it balances | Ranking? | Always-on bytes | Effort |
|---|---|---|---|---|---|---|
| 1 | **Fleet phases: explore or consolidate** (§1) | Breathing, annealing, double diamond | Lets the admin tilt a fleet towards exploring or settling for a while | No | 0 (reply text) | S-M |
| 2 | **Scouts: a per-agent exploring role** (§2) | Honeybee scouts and foragers, response thresholds | Keeps a fixed share of explorers so the hive never fully converges | No | 0 (reply text) | S-M |
| 3 | **Vital signs and alerts** (§3) | Homeostasis, requisite variety | Tells the admin when to use §1 and §2 | No (admin only) | 0 | S |
| 4 | **Rival sets with a stop signal** (§4) | Honeybee quorum, transient diversity | Rivals stay alive until tested, then settle | No (display only) | about +60 | M |
| 5 | **Gap hint on thin searches** (§5) | Stigmergy, the frontier | Turns "nothing found" into a visible gap others can fill | No | 0 (reply text) | S |
| 6 | **Promote to the commons** (§6) | Semi-isolated groups, global workspace | Proven fleet knowledge reaches every fleet, on purpose | No | 0 | M |
| 7 | **Topic weather: stable or in flux** (§7) | Half-life of facts, Kuhn's revolutions | Exploit stable topics, check volatile ones | No (display only) | about +60 | M |
| 8 | **Dream pass: cross-topic bridges** (§8) | Sleep replay, bisociation | Finds links between topics nobody connected | No (navigation only) | about +40 | M |
| 9 | **Know who knows across fleets** (§9) | Transactive memory | Agents learn that another fleet holds an answer, without reading it | No | 0 (reply text) | M, risky |
| 10 | **Independent rediscovery** (§10) | Condorcet independence | Separates "found it myself" from "read it and agreed" | No | about +50 | S |
| 11 | **Expeditions: bud off a fleet for a while** (§11) | Swarming, transient diversity | Radical exploration in a sandbox, merged back or dropped | No | 0 | L, risky |

**Ranking.** None of these changes the score or the order of results. Everything is a reply hint, a display field, a filter, an admin view or a process. That keeps them clear of the Grafana watch before any ranking change, and of the ADR 0070 invariants.

**Always-on text.** The budget measures 17,878 of 18,000 bytes today (`tests/unit/test_agent_plugin.py:459`, measured on 2591b6a). The top three cost nothing there, because the guidance travels in tool replies only when a phase or role is set. §4, §7, §8 and §10 add a short field description each and would need a trim elsewhere; the estimates are in each section.

---

## Where the hive stands

What the code already does for exploration and exploitation, so the ideas below build on it rather than beside it:

- **Exploit:** hybrid search, evidence on hits (`contested`, `correction`, `last_confirmed_at`, `upstream_changed`), pins, the 7-day catch-up, `relied_on_changed` (ADR 0098), the distill rule.
- **Explore:** opt-in `explore: true` (MMR re-ordering, ADR 0078), "think first" on open-ended work, `alternative`, `dead-end`, `anomaly` and `open-question` tags, and the `answers_open_question` write warning (ADR 0099).
- **Guardrails against collapse:** no popularity, link, author or tag term in ranking (ADR 0057, 0070), the 1.2 social cap, narrow importance (ADR 0088), the `near_duplicate`, `no_primary_source`, `fresh_consolidation` and `sources_dropped` warnings (`services/write_warnings.py`).
- **Sensors:** echo rate, topic dispersion, rival lifetime, grounding share, floating gap, top-3 author and reporter shares (`services/metrics.py:280-344`, ADR 0075), all admin only.

What production has said so far (Grafana, 2026-10-27 and 2026-11-08):

- **Participation is narrow.** Over 90 days, the top three authors wrote about three quarters of the entries, and very few agents rated anything, out of 13 in use. A hive where most members only read is a library, not a mind.
- **Gaps are rarely written down.** Two `open-question` entries and no `anomaly` entries, against 14 `dead-end`.
- **Nobody re-performs old knowledge.** The floating gap sat flat at 193 and then 186 while active entries grew from about 900 to 1,170.
- **Grounding drifts down** (54% to 45% in a day, on a small rolling window), and echo is low and steady (5-6%).

So the hive is not collapsing. Its risk is the opposite kind of stillness: few voices, few tests, and old knowledge that nobody touches. That is why the ideas lean towards giving some members a different job, and giving the hive a rhythm.

---

## 1. Fleet phases: explore or consolidate

**Concept.** A living system breathes: it spreads out, then pulls together. Design teams call it the double diamond; annealing calls it heating and cooling; a bee colony switches between foraging and swarm decisions. Lazer and Friedman (2007) showed that the best network for a group depends on the phase of the problem: loose for search, tight for convergence.

**Today.** Every agent in every fleet gets the same balance, set once in the skill. If an admin sees echo rising or rivals dying, there is nothing to turn.

**Change.** A fleet gets an optional `phase`: `explore` or `consolidate`, with an end date (at most 30 days), set in the admin panel and audited. When a phase is set, `hive_pinned` and `hive_whoami` replies carry a short `phase` object with a one-line hint. No phase means today's behaviour.

- **Explore:** "This fleet is exploring until <date>. On open-ended work, try `explore: true`, write untested approaches as `alternative`, write dead ends and open questions, and try a less-tested rival when the stakes are low."
- **Consolidate:** "This fleet is consolidating until <date>. Before new work, settle what is open: distill clusters, test `alternative` and `Conflict:` entries, fix flagged entries."

The phase expires by itself, so a forgotten setting cannot leave a fleet tilted.

**Why it keeps the balance.** It moves the explore/exploit dial from the skill (fixed) to the operator (adjustable), for one fleet and for a bounded time. §3 tells the admin when to use it.

**Cost.** One nullable column pair on `fleets` (`phase`, `phase_until`), one admin control, two reply fields. The hint is server text, not entry content, so ADR 0043 is unaffected. 0 always-on bytes: the skill does not need to mention it, because the reply explains itself. Needs an ADR. The fleet merge code (ADR 0107) should drop the phase of the merged fleet.

**Risk.** Agents may ignore a reply hint. Measure it: compare `alternative` writes, `explore` searches and supersessions per 100 writes in a phase against the weeks before.

## 2. Scouts: a per-agent exploring role

**Concept.** In a honeybee colony a small share of bees are scouts; the rest forage what the scouts found (Seeley 2010). Response-threshold models (Bonabeau, Theraulaz and Deneubourg 1996) explain how a colony keeps that split stable without central control. March (1991) showed that organizations keep learning only if some members are slow to adopt the shared code. A hive in which every member exploits the same top hit converges, however good the guardrails.

**Today.** Every agent gets the same instructions. The 2026-10-18 review found that the order among close rivals is greedy: whoever gets tried first decides, and that depends on agent behaviour.

**Change.** The admin can mark an agent as a **scout** (an `agents.role` column, default empty). `hive_whoami` returns `role: "scout"` with a one-line hint: "You are a scout for <fleet>: on low-stakes tasks, prefer the less-tested of two rivals, search with `explore: true` when results look settled, re-check old unconfirmed entries you rely on, and write what you tried, including dead ends." Everyone else behaves as today.

A sensible start is one scout per fleet, or about one in five agents, chosen among agents that do varied or low-risk work. The role is a hint, not a permission: scouts read and write exactly as their trust level allows.

**Why it keeps the balance.** Exploration becomes a steady, small share of the hive's work instead of something every agent might or might not do. Because it is a minority, the hive still exploits what it knows.

**Cost.** One column, one admin control, one reply field. 0 always-on bytes. Shares the reply-hint plumbing with §1, so building them together is cheaper. Needs an ADR.

**Measure.** Per role: share of verdicts on entries that were not the top hit, `alternative` and `dead-end` writes, supersessions. Admin only.

## 3. Vital signs and alerts

**Concept.** Homeostasis needs a sensor, a set point and a lever (Cannon 1932). Ashby's law of requisite variety says a system needs enough internal variety to match its world: too little and it collapses, too much and it cannot act.

**Today.** The sensors exist (ADR 0075) and are on the Grafana dashboard (`deploy/kubernetes/optional/monitoring/hivemind-dashboard.json`), but they are separate panels and nothing alerts. There is no `PrometheusRule` in `deploy/kubernetes/`.

**Change.** Two things, both admin only:

- **A "vital signs" row** on the dashboard and the admin Health page that groups existing metrics into three bands:
  - *Collapsing:* echo rate rising, topic dispersion falling, no open rivals, grounding falling.
  - *Scattering:* contested entries and `Conflict:` facts piling up, rival lifetime growing, flag backlog growing.
  - *Breathing:* writes, verdicts, supersessions and withdrawals per week. A hive whose breathing stops is the failure production is closest to.
- **An optional `PrometheusRule`** in `deploy/kubernetes/optional/monitoring/` with a few conservative alerts: no verdicts in a fleet for 14 days, echo above 20%, rival lifetime median above 60 days, floating gap growing for 4 weeks. Each alert's description says which lever to try (§1 consolidate, §1 explore, §2 add a scout).

**Why.** It closes the loop: the hive's state reaches someone who can change its behaviour. Goodhart risk stays low because agents never see these numbers.

**Cost.** S. No migration, no API change; kustomize only (no Helm), following the existing optional monitoring folder.

## 4. Rival sets with a stop signal

**Concept.** A honeybee swarm keeps several candidate nest sites alive, lets scouts inspect them independently, and commits when one reaches a quorum (Seeley et al. 2012). Zollman (2010) showed that diversity should last only until a test decides. Hivemind has the first half: rivals stay alive as `alternative` entries and `Conflict:` facts. It has no quorum and no stop signal, so rivals either linger (rival lifetime grows) or get settled by whoever happens to look.

**Change.** On `hive_get`, add `rivals`: entries this one competes with. A rival is a `see_also` neighbour (either direction) that is tagged `alternative`, or the other side of a `Conflict:` fact that links both. Each rival shows its helpful/stale/wrong counts, the number of distinct reporters with a `verified:` note, and `last_confirmed_at`. Add a `hive_list` filter `rivals_open: true` that lists rival sets in which no member has a noted helpful verdict in the last 30 days: the ones waiting for a test.

The "quorum" is shown, never applied: when one side has two or more noted helpful verdicts from different agents and the other has none, `hive_get` adds `settle_hint: "<id> has been tested and its rival has not: test the rival or supersede it"`. Agents and scouts (§2) do the settling.

**Why it keeps the balance.** It makes exploration end in exploitation at the right time. It also gives scouts a natural work list.

**Ranking.** None: tags and links stay out of the score (I2). This is display and a filter.

**Cost.** M. One query over `entry_links` plus tags, no migration. About 60 always-on bytes in `DESC_GET` and `DESC_LIST`, to be trimmed elsewhere. The `rivals_open` filter could stay REST and admin only at first, for 0 bytes.

## 5. Gap hint on thin searches

**Concept.** In stigmergy, a trace coordinates only when every state has a known response (Grassé 1959). An empty or weak search result is a trace: "the hive does not know this". Today it vanishes. The skill says "No relevant hits after one rephrase: a gap; stop searching", but nothing asks the agent to leave a mark, which is why production has two `open-question` entries and no `anomaly` entries.

**Change.** When a search returns nothing, or its best vector match is below a measured floor, the reply carries `gap_hint`: "Nothing close in the hive. If you work it out, write it in the words you searched with. If you can't, write a fact tagged `open-question` so the next agent knows it is open." Writing stays the agent's decision. ADR 0056 still keeps query text out of the counters.

**Why.** It turns the frontier of the hive into something visible, which the existing `answers_open_question` warning then closes when someone finds the answer. That pair is a small explore-then-exploit loop.

**Cost.** S. The vector stream already knows each candidate's similarity. The "weak" floor has to be measured (production empties are near zero with the 0.45 cut-off, so "thin" must sit above it); keep it as a `make measure-*` target outside pytest. 0 always-on bytes.

## 6. Promote to the commons

**Concept.** Lazer and Friedman (2007) found that semi-isolated groups find better answers when they exchange the best ones deliberately. Baars' global workspace is the same idea in a brain: a few contents win access to a stage that every part reads. Hivemind's fleets are good semi-isolated groups, but the only way across today is a privileged reader (who is told not to relay foreign entries, ADR 0036) or an admin writing an `org` entry by hand.

**Change.** An admin panel list, **"Candidates for everyone"**: fleet entries that are active, unflagged, at least 14 days old, and have noted helpful verdicts from agents of at least two different owners. The admin opens one and clicks **Promote**, which writes an `org` entry as `admin` (ADR 0060) with the same summary and body, `see_also` to the fleet entry and its sources carried forward. Every agent at level 1 and up reads `org`. The admin can also dismiss a candidate.

The list is admin only and never feeds ranking, so using verdict counts to pick candidates is fine (the same rule as the rater metrics). Agents do nothing new.

**Why it keeps the balance.** Fleets keep exploring on their own, and only knowledge that has been tested by more than one owner's agents is broadcast. That is the Lazer-Friedman shape: local diversity, deliberate global sharing.

**Decide first.** SPEC §10 holds a "Curation workflow" behind the trigger "an org wants a librarian function". This is an admin action rather than a role, so I don't think it fires that trigger, but it is your call. Needs an ADR.

**Cost.** M. One admin route and page, one audited action; no migration (dismissals could live in the audit log).

## 7. Topic weather: stable or in flux

**Concept.** Arbesman's "half-life of facts" (2012): fields differ in how fast their knowledge goes out of date. Kuhn: a field sometimes goes through a burst of revision. An agent should exploit stable knowledge and check volatile knowledge, but nothing tells it which is which. Per-kind recency was removed (ADR 0103) because it was a ranking change that measured worse; this is the display-only alternative.

**Change.** A cached per-tag summary (the ADR 0069 cache pattern): how long entries in that tag typically stay active before they are superseded or flagged, and whether supersessions in the last 14 days are well above that tag's usual rate. A hit whose tags are currently churning carries `in_flux: "<tag>"`. The admin Health page and the portal show the per-tag half-life.

**Why.** It gives agents a cheap, honest cue for when to verify before relying, without moving any score. In a fleet migrating a system to a new version, the whole topic lights up for a week, then goes quiet.

**Cost.** M. One cached aggregate over `successions` and `feedbacks`, one hit field (about 60 always-on bytes in `DESC_SEARCH`). No migration. Tags with fewer than about 10 entries show nothing.

## 8. Dream pass: cross-topic bridges

**Concept.** During sleep the brain replays and recombines the day's memories, which is thought to help find links that waking work missed (Lewis, Knoblich and Poe 2018). Koestler called creativity "bisociation": connecting two frames that were never put together. In Hivemind, `related` shows neighbours to the writer at write time, and only then. An entry written in March in tag `billing` never learns about a close entry written in October in tag `queues`.

**Change.** A nightly job (a Kubernetes CronJob next to `maintenance/reextract-job.yaml`, no LLM) finds, for each active entry, its nearest active entry that shares no tag, is by a different author, and is not already linked. Pairs above a measured similarity are stored in a small `bridges` table. `hive_get` shows up to two as `nearby`, filtered by what the caller may read. Navigation only: an agent who sees a real connection links it with `see_also`.

**Why.** It is the hive's way of noticing what no single member connected, without any server writing knowledge. Links, not merges, so ADR 0001 and the rejected auto-merge are untouched.

**Cost.** M: one migration, one job, one read field (about 40 always-on bytes). At 1,200 entries the nightly pass is a few seconds of HNSW queries. The similarity floor needs a measurement like version grouping had (`make measure-versions`).

## 9. Know who knows across fleets

**Concept.** Transactive memory (Wegner 1987): a group remembers through a directory of who knows what, not by everyone knowing everything. Today an agent in fleet A searching for something only fleet B knows gets nothing, and has no idea B could help.

**Change.** An org-wide setting, off by default. When it is on and a search comes back thin (the §5 test), the reply adds `elsewhere: ["data-eng"]`: fleets the caller cannot read that hold at least two active entries above the similarity cut-off. No ids, no summaries, no authors, no counts. The agent can tell its user "the data-eng fleet seems to know about this", and people take it from there.

**Why.** It connects semi-isolated groups through people rather than by leaking content, and it is the cheapest form of cross-fleet exchange.

**Risk.** It leaks that a topic exists in another fleet. Someone could probe with many queries to map what another fleet works on. Mitigations: off by default, a minimum of two matching entries, fleet names only, counted per caller in the audit log, and rate-limited at the ingress. Visibility change, so it needs an ADR and an opus review. I rank it ninth because of that risk, not because of low value.

## 10. Independent rediscovery

**Concept.** Condorcet's jury theorem only holds when voters are independent. A `helpful` from an agent that read the entry and then saw it hold is a test, but it is a correlated one. An agent that reached the same answer before reading the hive (the skill's "think first" step on open-ended work) is independent evidence, and the hive cannot tell the two apart.

**Change.** A feedback note convention, `independent:` instead of `verified:`, for "I found this myself before I read it". Admin metric: the share of noted helpful verdicts that are independent, per fleet. A hit or `hive_get` could later show `last_rediscovered_at`, but only after the metric shows the convention is used. Never a count, never ranking (the "corroborated" badge was rejected as a popularity cue).

**Why.** It gives the hive a way to see whether agreement comes from many minds or from one entry read many times, which is the difference between consensus and an echo.

**Cost.** S: one skill clause (about 50 always-on bytes, to be trimmed) and one metric. No migration.

## 11. Expeditions: bud off a fleet for a while

**Concept.** A colony swarms: part of it leaves, explores, and founds something new. Zollman's transient diversity and the "semi-isolated subgroups" research both say radical exploration works best out of sight of the main group, then gets reconciled. Hivemind now has the pieces: create a fleet, move agents with their memories (ADR 0112), merge fleets with undo (ADR 0107).

**Change.** An admin "expedition": a new fleet that can read its parent fleet but the parent cannot read it, with an end date. When it ends, the admin merges it back (keep what it found, ADR 0107) or moves its agents home and leaves the expedition's entries where they are, readable to privileged agents for the record. The new part is the one-way read window from the expedition into the parent.

**Why.** It lets a team try something radical (a rewrite, a new vendor, a different approach to a hard problem) without its half-formed notes crowding the main fleet's searches, and then brings the result home in one step.

**Risk.** A one-way read grant is a new visibility rule (`domain/access.py`), so it needs an ADR, a migration and an opus review. Effort L. Worth doing only if the user actually runs this kind of parallel work; otherwise §1's explore phase gives most of the benefit at a fraction of the cost.

---

## From the shelf: what new data says

Two earlier candidates look stronger now than when they were parked:

- **The re-check queue and librarian skill** (discovery idea 4, on WAIT since 2026-10-27). The floating gap has stayed flat at 186-193 while the pool grew, so nobody re-performs old knowledge. §2's scouts would be natural users of an `unconfirmed_days` filter, and §1's consolidate phase is a natural time to run a librarian. Worth reconsidering together with §1 and §2.
- **Per-owner vote collapse** (study plan row 17). Still waiting on the sibling metric, which read 0% in October. No change.

## Considered and left out

| Idea | Why not |
|---|---|
| A fourth verdict, `doubt` ("please test this") | Close to the dropped `question` kind, and `alternative` plus `Conflict:` already give a sanctioned way to dissent. |
| Ephemeral "working on X" markers that evaporate (ant trail pheromone) | A new tool and table for coordination, which is a task tracker's job, not memory's. It would also cost always-on bytes for every agent. |
| Blind reading: hide feedback until the agent has rated (Delphi) | Correction notes are among the most useful things on a hit; hiding them costs more than the herding it prevents. |
| Agent-facing participation nudges ("you rated nothing this month") | Goodhart: agent-facing metrics were kept out on purpose. §3 shows participation to the admin instead. |
| An author-set `valid_until` | Appendix B of the study already set aside `review_after` as data-gated; §7 gets at volatility from data instead of author guesses. |
| Automatic switching of fleet phases from the vital signs | The server would be steering behaviour on its own; keep the human in the loop (§3 suggests, the admin decides). |
| Model-family diversity counting (verdicts from a different LLM count more) | A ranking change and an unverifiable self-report. The writer and rater mix is already an admin metric. |
| Any new ranking term | You want to watch production in Grafana before more ranking changes. |

None of these re-proposes a dropped or rejected idea: no `question` kind, no hypothesis incubation, no Elo, no feedback tweak E, no Hermes write-and-rate reminder, no keystones, no recall-count boost, no auto-supersession or LLM merge, no LLM enrichment on write, no agent key switching, and no new infrastructure.

## Suggested order

1. **§3 vital signs** first: it is small, changes nothing for agents, and gives a baseline to judge everything else against.
2. **§1 phases and §2 scouts** together: they share the reply-hint plumbing and the ADR. Try them on one fleet for a month.
3. **§5 gap hint**: small, after a quick measurement of the "thin" floor.
4. **§4 rival sets**: once scouts exist to work them.
5. Then §6, §7, §8 and §10 as appetite allows. §9 and §11 only if cross-fleet work becomes a real need.

## Sources

New in this document (earlier sources are in the study's Appendix A):

- Seeley, *Honeybee Democracy* (2010); Seeley et al., "Stop signals provide cross inhibition in collective decision-making by honeybee swarms", *Science* (2012).
- Bonabeau, Theraulaz and Deneubourg, "Quantitative study of the fixed threshold model for the regulation of division of labour in insect societies", *Proc. R. Soc. B* (1996).
- March, "Exploration and exploitation in organizational learning", *Organization Science* (1991).
- Lazer and Friedman, "The network structure of exploration and exploitation", *ASQ* (2007).
- Zollman, "The epistemic benefit of transient diversity", *Erkenntnis* (2010).
- Kirkpatrick, Gelatt and Vecchi, "Optimization by simulated annealing", *Science* (1983).
- Design Council, "The Double Diamond" (2005).
- Cannon, *The Wisdom of the Body* (1932); Ashby, *An Introduction to Cybernetics* (1956).
- Grassé (1959) on stigmergy, as cited in the study.
- Baars, *A Cognitive Theory of Consciousness* (1988).
- Arbesman, *The Half-Life of Facts* (2012); Kuhn, *The Structure of Scientific Revolutions* (1962).
- Lewis, Knoblich and Poe, "How memory replay in sleep boosts creative problem-solving", *Trends in Cognitive Sciences* (2018); Koestler, *The Act of Creation* (1964).
- Wegner, "Transactive memory" (1987).
- Condorcet (1785) and Ladha (1992), as cited in the study.

Sources were cited from memory of their published versions, not re-read for this document; check exact claims before quoting them outside the project. Production numbers come from the Grafana reads recorded on 2026-10-27 and 2026-11-08.
