"""Usage counts, fleet health, activity and convergence reports."""

from __future__ import annotations

from datetime import datetime

from hivemind.domain.activity import (
    Activity,
    AuthorActivity,
    DayActivity,
    ReceivedFeedback,
    ReporterActivity,
)
from hivemind.domain.entry import ImportanceSource, Kind, UsageCount
from hivemind.domain.health import (
    DISPERSION_CAP,
    DISPERSION_MIN,
    DISPERSION_TAGS,
    ECHO_SAMPLE,
    ECHO_THRESHOLD,
    ENTRY_ID_REF,
    Convergence,
    FleetHealth,
    HealthCounts,
    TagDispersion,
)
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import ages
from hivemind.store.pgstore.sql_common import _RATING_COUNTS, VECTOR_SEARCH_SETTINGS

# The grouped usage counters: one scan instead of one COUNT per figure.
USAGE_COUNTS = """
SELECT scope, kind, importance_source, author, fleet_id,
       (state = 'active') AS active, count(*) AS n
  FROM entries
 GROUP BY scope, kind, importance_source, author, fleet_id, (state = 'active')
"""

# The admin activity view (ADR 0080): one grouped scan per figure; ``$1`` is the window start.
# Revoked reporters are left out (ADR 0067).
ACTIVITY_AUTHORS = """
SELECT author, fleet_id, count(*) AS entries,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE created_at >= $1) AS recent,
       max(created_at) AS last_write_at
  FROM entries GROUP BY author, fleet_id
"""

ACTIVITY_REPORTERS = f"""
SELECT f."user" AS reporter, count(*) AS verdicts,
       count(*) FILTER (WHERE f.updated_at >= $1) AS recent,
       count(*) FILTER (WHERE f.verdict = 'helpful') AS helpful,
       count(*) FILTER (WHERE f.verdict = 'stale') AS stale,
       count(*) FILTER (WHERE f.verdict = 'wrong') AS wrong,
       max(f.updated_at) AS last_at
  FROM feedbacks f WHERE {_RATING_COUNTS}
 GROUP BY f."user"
"""

ACTIVITY_RECEIVED = f"""
SELECT e.author, count(*) FILTER (WHERE f.verdict = 'helpful') AS helpful,
       count(*) FILTER (WHERE f.verdict = 'stale') AS stale,
       count(*) FILTER (WHERE f.verdict = 'wrong') AS wrong
  FROM feedbacks f JOIN entries e ON e.id = f.entry_id
 WHERE {_RATING_COUNTS}
 GROUP BY e.author
"""

ACTIVITY_DAYS = f"""
SELECT day, sum(writes)::int AS writes, sum(helpful)::int AS helpful,
       sum(stale)::int AS stale, sum(wrong)::int AS wrong
  FROM (
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, 1 AS writes,
           0 AS helpful, 0 AS stale, 0 AS wrong
      FROM entries WHERE created_at >= $1
    UNION ALL
    SELECT (f.updated_at AT TIME ZONE 'UTC')::date, 0,
           (f.verdict = 'helpful')::int, (f.verdict = 'stale')::int, (f.verdict = 'wrong')::int
      FROM feedbacks f WHERE f.updated_at >= $1 AND {_RATING_COUNTS}
  ) t
 GROUP BY day ORDER BY day
"""

# Fleet health metrics; $1 is "now". Four statements share one snapshot (see
# ``fleet_health``), each dominated by a pass over the wide ``entries`` heap: fusing
# them through a materialised CTE spills past work_mem and is slower. Windows are
# hour-based so they ignore the server TimeZone.
#
# The per-fleet counters are two passes summed per fleet: the active entries, and the
# standing stale/wrong verdicts joined to their entries (one index probe per author
# for the last write).
HEALTH_BY_FLEET = f"""
WITH flagged AS (
    SELECT e.id, e.fleet_id, e.author, min(f.updated_at) AS first_flag
      FROM feedbacks f JOIN entries e ON e.id = f.entry_id
     WHERE f.verdict IN ('stale', 'wrong') AND e.state = 'active' AND {_RATING_COUNTS}
     GROUP BY e.id
), last_write AS (
    SELECT fa.author,
           (SELECT max(w.created_at) FROM entries w WHERE w.author = fa.author) AS at
      FROM (SELECT DISTINCT author FROM flagged) fa
), fresh AS (
    SELECT DISTINCT f.entry_id FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
), linking AS (
    SELECT from_id FROM entry_links GROUP BY from_id HAVING count(*) >= 2
), parts AS (
    SELECT e.fleet_id,
           count(*) FILTER (WHERE e.scope IN ('org', 'fleet')
                              AND e.kind IN ('fact', 'insight')) AS sourceable,
           count(*) FILTER (WHERE e.scope IN ('org', 'fleet')
                              AND e.kind IN ('fact', 'insight')
                              AND jsonb_array_length(e.sources) > 0) AS sourced,
           count(*) FILTER (WHERE e.kind = 'insight') AS insights,
           count(*) FILTER (WHERE e.kind = 'insight' AND linking.from_id IS NOT NULL) AS synthesizing,
           0::bigint AS backlog, 0::bigint AS orphaned,
           count(*) FILTER (WHERE e.occurred_at < $1::timestamptz - interval '2160 hours'
                              AND fresh.entry_id IS NULL) AS floating,
           count(*) FILTER (WHERE 'anomaly' = ANY(e.tags)) AS gap_anomaly,
           count(*) FILTER (WHERE 'open-question' = ANY(e.tags)) AS gap_open_question,
           count(*) FILTER (WHERE 'dead-end' = ANY(e.tags)) AS gap_dead_end
      FROM entries e
      LEFT JOIN fresh ON fresh.entry_id = e.id
      LEFT JOIN linking ON linking.from_id = e.id
     WHERE e.state = 'active'
     GROUP BY e.fleet_id
  UNION ALL
    SELECT fl.fleet_id, 0, 0, 0, 0,
           count(*) FILTER (WHERE fl.first_flag < $1::timestamptz - interval '336 hours'),
           count(*) FILTER (WHERE a.status = 'revoked'
                              OR lw.at < $1::timestamptz - interval '720 hours'),
           0, 0, 0, 0
      FROM flagged fl
      LEFT JOIN agents a ON a.name = fl.author
      LEFT JOIN last_write lw ON lw.author = fl.author
     GROUP BY fl.fleet_id
)
SELECT fleet_id, sum(sourceable) AS sourceable, sum(sourced) AS sourced,
       sum(insights) AS insights, sum(synthesizing) AS synthesizing,
       sum(backlog) AS backlog, sum(orphaned) AS orphaned, sum(floating) AS floating,
       sum(gap_anomaly) AS gap_anomaly, sum(gap_open_question) AS gap_open_question,
       sum(gap_dead_end) AS gap_dead_end
  FROM parts
 GROUP BY fleet_id
"""


# An owner alias as ADR 0089 compares it (trimmed, lower-cased, blank = none);
# ``owner_key`` in ``domain/health.py`` is the same rule for ``MemoryStore``.
def _owner_key(column: str) -> str:
    return f"nullif(lower(btrim({column}, E' \\t\\r\\n\\f\\v')), '')"


# Fleet-wide scalars in one round trip: 90-day writes and verdicts with top-3
# concentration, 30-day figures by agent label, insight grounding ($2 is the pattern
# of a source ref that only names an entry), supersessions (counted when the successor
# was written), self -> fleet promotions, and the owner-level verdicts (ADR 0089).
HEALTH_SCALARS = f"""
WITH writes AS (
    SELECT author, count(*) AS n FROM entries
     WHERE created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
     GROUP BY author
), verdicts AS (
    SELECT f."user" AS reporter, count(*) AS n FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
     GROUP BY f."user"
), owners AS (
    SELECT CASE WHEN o.key IS NOT NULL THEN 'o:' || o.key ELSE 'r:' || f."user" END AS owner,
           count(*) AS n
      FROM feedbacks f
      LEFT JOIN LATERAL (SELECT {_owner_key("a.owner_alias")} AS key
                           FROM agents a WHERE a.name = f."user") o ON true
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
     GROUP BY 1
), siblings AS (
    SELECT count(*) AS n
      FROM feedbacks f
      JOIN entries e ON e.id = f.entry_id
      JOIN agents ra ON ra.name = f."user"
      JOIN agents ea ON ea.name = e.author
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
       AND ra.name <> ea.name
       AND {_owner_key("ra.owner_alias")} = {_owner_key("ea.owner_alias")}
), helpful AS (
    SELECT count(*) AS n, count(*) FILTER (WHERE f.note ~ '\\S') AS noted
      FROM feedbacks f WHERE f.verdict = 'helpful' AND {_RATING_COUNTS}
), grounding AS (
    SELECT count(*) AS n,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM jsonb_array_elements(e.sources) src
                WHERE src->>'ref' !~* $2)) AS grounded
      FROM entries e
     WHERE e.created_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND e.scope IN ('org', 'fleet') AND e.kind IN ('insight', 'decision')
), writes30 AS (
    SELECT agent, count(*) AS n FROM entries
     WHERE created_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND agent ~ '\\S'
     GROUP BY agent
), verdicts30 AS (
    SELECT f.agent, count(*) AS n FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND f.agent ~ '\\S' AND {_RATING_COUNTS}
     GROUP BY f.agent
), chains AS (
    SELECT count(*) AS supersessions,
           count(DISTINCT n.id) FILTER (WHERE n.scope = 'fleet' AND s.scope = 'self') AS promotions
      FROM entries n JOIN entries s ON s.superseded_by = n.id
     WHERE n.created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
)
SELECT (SELECT n FROM helpful) AS helpful,
       (SELECT noted FROM helpful) AS helpful_noted,
       (SELECT coalesce(sum(n), 0) FROM writes) AS writes,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM writes ORDER BY n DESC LIMIT 3) t) AS top_authors,
       (SELECT count(*) FROM writes) AS authors,
       (SELECT coalesce(sum(n), 0) FROM verdicts) AS verdicts,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM verdicts ORDER BY n DESC LIMIT 3) t) AS top_reporters,
       (SELECT count(*) FROM verdicts) AS reporters,
       (SELECT n FROM siblings) AS sibling_verdicts,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM owners ORDER BY n DESC LIMIT 3) t) AS top_owners,
       (SELECT count(*) FROM owners) AS owners,
       (SELECT supersessions FROM chains) AS supersessions,
       (SELECT promotions FROM chains) AS promotions,
       (SELECT n FROM grounding) AS synthesis_writes,
       (SELECT grounded FROM grounding) AS grounded,
       (SELECT coalesce(sum(n), 0) FROM writes30) AS writes30,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM writes30 ORDER BY n DESC LIMIT 3) t) AS top_write_labels,
       (SELECT count(*) FROM writes30) AS write_labels,
       (SELECT coalesce(sum(n), 0) FROM verdicts30) AS verdicts30,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM verdicts30 ORDER BY n DESC LIMIT 3) t) AS top_verdict_labels,
       (SELECT count(*) FROM verdicts30) AS verdict_labels
"""

# Ages in seconds of active entries showing live rivals: tagged ``alternative``,
# a summary starting "Conflict:", or contested (helpful beside stale/wrong).
HEALTH_RIVALS = f"""
SELECT 'alternative' AS kind, extract(epoch FROM ($1::timestamptz - e.created_at))::float8 AS age
  FROM entries e WHERE e.state = 'active' AND 'alternative' = ANY(e.tags)
UNION ALL
SELECT 'conflict', extract(epoch FROM ($1::timestamptz - e.created_at))::float8
  FROM entries e WHERE e.state = 'active' AND e.summary LIKE 'Conflict:%'
UNION ALL
SELECT 'contested', extract(epoch FROM ($1::timestamptz - e.created_at))::float8
  FROM entries e
 WHERE e.state = 'active'
   AND e.id IN (SELECT f.entry_id FROM feedbacks f WHERE {_RATING_COUNTS}
                 GROUP BY f.entry_id
                HAVING bool_or(f.verdict = 'helpful') AND bool_or(f.verdict <> 'helpful'))
"""

# Echo rate: for each of the newest $2 writes of 30 days, the nearest earlier active
# entry (one HNSW lookup, search's settings); an echo when similarity >= $3.
CONVERGENCE_ECHO = """
WITH w AS (
    SELECT id, embedding, created_at FROM entries
     WHERE created_at >= $1::timestamptz - interval '720 hours' AND created_at <= $1::timestamptz
       AND embedding IS NOT NULL
     ORDER BY created_at DESC, id DESC LIMIT $2
)
SELECT count(*) AS sampled, count(*) FILTER (WHERE nn.sim >= $3::float8 AND nn.sim <> 'NaN'::float8) AS echoes
  FROM w
  LEFT JOIN LATERAL (
      SELECT 1 - (e.embedding <=> w.embedding) AS sim FROM entries e
       WHERE e.state = 'active' AND e.embedding IS NOT NULL
         AND (e.created_at, e.id) < (w.created_at, w.id)
       ORDER BY e.embedding <=> w.embedding LIMIT 1) nn ON true
"""

# Topic dispersion: the $3 busiest org/fleet tags with >= $4 entries in both 30-day
# windows (chosen without reading vectors), the $2 newest entries per tag and window,
# then the mean pairwise cosine per window (a zero-norm vector counts as 0). Vectors
# are normalised once on fetch: the column is TOASTed and detoasting it per pair was
# most of the cost.
CONVERGENCE_DISPERSION = """
WITH tagged AS (
    SELECT DISTINCT t.tag, e.id, e.created_at,
           (e.created_at >= $1::timestamptz - interval '720 hours') AS recent
      FROM entries e, unnest(e.tags) AS t(tag)
     WHERE e.created_at BETWEEN $1::timestamptz - interval '1440 hours' AND $1::timestamptz
       AND e.embedding IS NOT NULL AND e.scope IN ('org', 'fleet')
), busiest AS (
    SELECT tag, count(*) AS n, count(*) FILTER (WHERE recent) AS n_recent,
           count(*) FILTER (WHERE NOT recent) AS n_before
      FROM tagged GROUP BY tag
    HAVING count(*) FILTER (WHERE recent) >= $4 AND count(*) FILTER (WHERE NOT recent) >= $4
     ORDER BY n DESC, tag COLLATE "C" LIMIT $3
), capped AS (
    SELECT tag, id, recent FROM (
        SELECT t.tag, t.id, t.recent,
               row_number() OVER (PARTITION BY t.tag, t.recent
                                  ORDER BY t.created_at DESC, t.id DESC) AS rn
          FROM tagged t JOIN busiest b ON b.tag = t.tag) ranked
     WHERE rn <= $2
), vectors AS MATERIALIZED (
    SELECT c.tag, c.id, c.recent,
           (SELECT l2_normalize(e.embedding) FROM entries e WHERE e.id = c.id) AS embedding
      FROM capped c
), pairs AS (
    SELECT a.tag, a.recent,
           avg(CASE WHEN d.dist = 'NaN'::float8 THEN 0 ELSE 1 - d.dist END) AS cosine
      FROM vectors a
      JOIN vectors b ON b.tag = a.tag AND b.recent = a.recent AND a.id < b.id
      CROSS JOIN LATERAL (SELECT a.embedding <=> b.embedding AS dist) d
     GROUP BY a.tag, a.recent
)
SELECT b.tag, least(b.n_recent, $2) AS n_recent, least(b.n_before, $2) AS n_before,
       max(p.cosine) FILTER (WHERE p.recent) AS recent_cosine,
       max(p.cosine) FILTER (WHERE NOT p.recent) AS before_cosine, b.n
  FROM busiest b JOIN pairs p ON p.tag = b.tag
 GROUP BY b.tag, b.n_recent, b.n_before, b.n
 ORDER BY b.n DESC, b.tag COLLATE "C"
"""

# Per superseded entry flagged before its successor (written in the last 90 days)
# existed: seconds from its first flag to the successor's ``created_at`` (ADR 0068).
HEALTH_CORRECTIONS = f"""
SELECT extract(epoch FROM (n.created_at - min(f.updated_at)))::float8 AS seconds
  FROM entries n
  JOIN entries s ON s.superseded_by = n.id AND s.state = 'superseded'
  JOIN feedbacks f ON f.entry_id = s.id
                  AND f.verdict IN ('stale', 'wrong')
                  AND f.updated_at < n.created_at
                  AND {_RATING_COUNTS}
 WHERE n.created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
 GROUP BY s.id, n.created_at
"""


class MetricsStore(PgConnections):
    async def usage_counts(self) -> list[UsageCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(USAGE_COUNTS)
        return [
            UsageCount(
                scope=row["scope"],
                kind=Kind(row["kind"]),
                importance_source=ImportanceSource(row["importance_source"]),
                author=row["author"],
                fleet_id=str(row["fleet_id"]) if row["fleet_id"] is not None else None,
                active=bool(row["active"]),
                count=int(row["n"]),
            )
            for row in rows
        ]

    async def fleet_health(self, now: datetime) -> FleetHealth:
        # One REPEATABLE READ snapshot so the figures agree; JIT is off (compiling costs more
        # than these queries save).
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL jit = off")
            by_fleet = await conn.fetch(HEALTH_BY_FLEET, now)
            scalars = await conn.fetchrow(HEALTH_SCALARS, now, ENTRY_ID_REF)
            corrections = await conn.fetch(HEALTH_CORRECTIONS, now)
            rivals = await conn.fetch(HEALTH_RIVALS, now)
        assert scalars is not None
        return FleetHealth(
            by_fleet=tuple(
                HealthCounts(
                    fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                    sourceable=int(r["sourceable"]),
                    sourced=int(r["sourced"]),
                    insights=int(r["insights"]),
                    synthesizing=int(r["synthesizing"]),
                    flagged_backlog_14d=int(r["backlog"]),
                    orphaned_flagged=int(r["orphaned"]),
                    floating_gap=int(r["floating"]),
                    gap_anomaly=int(r["gap_anomaly"]),
                    gap_open_question=int(r["gap_open_question"]),
                    gap_dead_end=int(r["gap_dead_end"]),
                )
                for r in by_fleet
            ),
            helpful=int(scalars["helpful"]),
            helpful_with_note=int(scalars["helpful_noted"]),
            correction_latencies=tuple(float(r["seconds"]) for r in corrections),
            writes_90d=int(scalars["writes"]),
            top3_author_writes_90d=int(scalars["top_authors"]),
            authors_90d=int(scalars["authors"]),
            verdicts_90d=int(scalars["verdicts"]),
            top3_reporter_verdicts_90d=int(scalars["top_reporters"]),
            reporters_90d=int(scalars["reporters"]),
            sibling_verdicts_90d=int(scalars["sibling_verdicts"]),
            top3_owner_verdicts_90d=int(scalars["top_owners"]),
            owners_90d=int(scalars["owners"]),
            supersessions_90d=int(scalars["supersessions"]),
            self_to_fleet_promotions_90d=int(scalars["promotions"]),
            alternative_ages=ages(rivals, "alternative"),
            conflict_ages=ages(rivals, "conflict"),
            contested_ages=ages(rivals, "contested"),
            synthesis_writes_30d=int(scalars["synthesis_writes"]),
            grounded_30d=int(scalars["grounded"]),
            writes_30d=int(scalars["writes30"]),
            top3_label_writes_30d=int(scalars["top_write_labels"]),
            write_labels_30d=int(scalars["write_labels"]),
            verdicts_30d=int(scalars["verdicts30"]),
            top3_label_verdicts_30d=int(scalars["top_verdict_labels"]),
            verdict_labels_30d=int(scalars["verdict_labels"]),
        )

    async def activity(self, since: datetime) -> Activity:
        # One snapshot, like ``fleet_health``.
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL jit = off")
            authors = await conn.fetch(ACTIVITY_AUTHORS, since)
            reporters = await conn.fetch(ACTIVITY_REPORTERS, since)
            received = await conn.fetch(ACTIVITY_RECEIVED)
            days = await conn.fetch(ACTIVITY_DAYS, since)
        return Activity(
            authors=tuple(
                AuthorActivity(
                    author=r["author"],
                    fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                    entries=int(r["entries"]),
                    active=int(r["active"]),
                    recent=int(r["recent"]),
                    last_write_at=r["last_write_at"],
                )
                for r in authors
            ),
            reporters=tuple(
                ReporterActivity(
                    reporter=r["reporter"],
                    verdicts=int(r["verdicts"]),
                    recent=int(r["recent"]),
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                    last_at=r["last_at"],
                )
                for r in reporters
            ),
            received=tuple(
                ReceivedFeedback(
                    author=r["author"],
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                )
                for r in received
            ),
            days=tuple(
                DayActivity(
                    day=r["day"],
                    writes=int(r["writes"]),
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                )
                for r in days
            ),
        )

    async def fleet_convergence(self, now: datetime) -> Convergence:
        async with self._transaction() as conn:
            await conn.execute("SET LOCAL jit = off")
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            echo = await conn.fetchrow(CONVERGENCE_ECHO, now, ECHO_SAMPLE, ECHO_THRESHOLD)
            rows = await conn.fetch(
                CONVERGENCE_DISPERSION, now, DISPERSION_CAP, DISPERSION_TAGS, DISPERSION_MIN
            )
        assert echo is not None
        return Convergence(
            echo_sampled=int(echo["sampled"]),
            echo_hits=int(echo["echoes"]),
            dispersion=tuple(
                TagDispersion(
                    tag=row["tag"],
                    recent_entries=int(row["n_recent"]),
                    before_entries=int(row["n_before"]),
                    recent_cosine=float(row["recent_cosine"]),
                    before_cosine=float(row["before_cosine"]),
                )
                for row in rows
            ),
        )
