# The fleet activity chart as bars stacked by agent, with the fleet's verdict mix

Amends [ADR 0165](0165-portal-fleet-activity-chart.md) (decision 1, the drawing, and decision 3, the verdict mix). Amends SPEC §5.1.

Status: Accepted.

## Context

With five or more agents, the per-agent lines of ADR 0165 cross each other on almost every day. That made the chart hard to read, and the log scale did not help with the crossings. The person wanted the chart to answer two questions first: is the fleet busier or quieter than before, and who does the work. They preferred bars to lines for both, and asked to see the mix of helpful, stale and wrong verdicts as well.

## Decision

1. **Bars stacked by agent.** Each UTC day is one bar. Its height is the fleet's writes (or ratings) that day, and its segments are the agents, with the colours, the seven-agent limit, the "Other agents" fold and the colour-follows-the-agent rule of ADR 0165. A tooltip that follows the pointer or the arrow keys lists the day's non-zero agents and the total. The chart is drawn at the width it is shown at, so its labels keep their size. The axis ticks whole counts. The **Linear | Log** toggle is removed: a stacked bar on a log scale misstates every segment above the first.
2. **Each agent's total, in name order.** Under the bars, every agent with activity has one row: its colour, its name, a bar and its count for the window. The rows are in name order, never by count, so the list is not a ranking. An agent folded into "Other agents" in the chart is still listed here by name, with the gray swatch.
3. **The fleet's verdict mix, never per agent.** In Ratings mode, a split bar shows the fleet's helpful, stale and wrong verdicts over the window, with counts and whole-number shares. The reply gains `verdicts: {helpful, stale, wrong}`: the sum over all members, on entries the key can read, with the same rules as `ratings`. The per-agent rows keep only `writes` and `ratings`. The point in ADR 0165 decision 3 still holds: nobody's own verdict mix is shown to the fleet. The store splits each member's day by verdict, and the service adds the counts up before they leave the server.
4. **Cost.** The ratings query is the same scan with three `count(*) FILTER` columns in place of one `count(*)`, so the cost and the cache of ADR 0165 decision 5 are unchanged.

## Consequences

- The height of a bar shows the fleet's trend, and its colours show who did the work, without lines crossing.
- A person who relied on the log scale to see quiet agents next to a busy one now reads them in the per-agent rows instead.
- A fleet member can see the fleet's verdict mix, but not the mix of any single agent.
