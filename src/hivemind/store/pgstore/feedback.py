"""Feedback: verdicts, counts, signals, the log and the flag backlog."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import Any

from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryState, Kind
from hivemind.domain.feedback import (
    Correction,
    Feedback,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    Verdict,
    last_confirmed,
    truncate_note,
)
from hivemind.domain.validation import canonical_uuid, is_valid_uuid
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import row_to_feedback
from hivemind.store.pgstore.sql_common import _RATING_COUNTS, add_clause, visibility_clause
from hivemind.store.pgstore.uuids import canonical_uuids

# ``updated_at`` defaults to the database clock, like ``created_at`` (ADR 0079).
UPSERT_FEEDBACK = """
INSERT INTO feedbacks (entry_id, "user", agent, verdict, note, updated_at)
VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
ON CONFLICT (entry_id, "user", agent) DO UPDATE
SET verdict = EXCLUDED.verdict,
    note = EXCLUDED.note,
    updated_at = EXCLUDED.updated_at,
    set_aside_by = NULL
RETURNING updated_at
"""

# Informative rows before bare helpful ones (ADR 0064), newest first.
_FEEDBACK_ORDER = (
    """(verdict = 'helpful' AND COALESCE(note, '') = ''), updated_at DESC, "user", agent"""
)

# The verdict counts shared by every counting statement.
_VERDICT_COUNTS = """COUNT(*) FILTER (WHERE verdict = 'helpful') AS helpful,
    COUNT(*) FILTER (WHERE verdict = 'stale') AS stale,
    COUNT(*) FILTER (WHERE verdict = 'wrong') AS wrong"""

COUNT_FEEDBACK = f"""
SELECT
    {_VERDICT_COUNTS}
FROM feedbacks f
WHERE entry_id = $1 AND {_RATING_COUNTS}
"""

LIST_FEEDBACK = f"""
SELECT entry_id, "user", agent, verdict, note, updated_at
FROM feedbacks f
WHERE entry_id = $1 AND {_RATING_COUNTS}
ORDER BY {_FEEDBACK_ORDER}
LIMIT $2
"""

# The first $2 rows of each entry in $1 (ADR 0059), in LIST_FEEDBACK order
# (informative rows before bare helpful ones, ADR 0064).
LIST_FEEDBACK_MANY = f"""
SELECT entry_id, "user", agent, verdict, note, updated_at FROM (
    SELECT f.*, row_number() OVER (
               PARTITION BY entry_id
               ORDER BY {_FEEDBACK_ORDER}) AS rn
      FROM feedbacks f
     WHERE entry_id = ANY($1::uuid[]) AND {_RATING_COUNTS}
) AS ranked
WHERE rn <= $2
ORDER BY entry_id, {_FEEDBACK_ORDER}
"""

# The feedback log: rows across entries with the rated entry's list fields.
# ``{where}`` is built by ``list_feedback_log``.
FEEDBACK_LOG = """
SELECT f.entry_id, f."user", f.agent, f.verdict, f.note, f.updated_at,
       e.kind, e.summary, e.author, e.scope, e.fleet_id, e.state
  FROM feedbacks f JOIN entries e ON e.id = f.entry_id
 WHERE {where}
 ORDER BY f.updated_at DESC, f.entry_id, f."user", f.agent
 LIMIT ${limit} OFFSET ${offset}
"""

# The flag backlog (ADR 0093). $1 is now; "orphaned" is HEALTH_BY_FLEET's rule.
# {inner} filters the entry, {outer} the listed row. {narrow} limits the grouped
# ratings to the fleet's or authors' entries: without it every rating in the pool
# is grouped on each read, which the portal's per-agent list cannot afford.
FLAG_BACKLOG = f"""
WITH flagged AS (
    SELECT f.entry_id,
           min(f.updated_at) FILTER (WHERE f.verdict IN ('stale', 'wrong')) AS first_flag,
           max(f.updated_at) FILTER (WHERE f.verdict IN ('stale', 'wrong')) AS last_flag,
           {_VERDICT_COUNTS}
      FROM feedbacks f
     WHERE {_RATING_COUNTS}{{narrow}}
     GROUP BY f.entry_id
    HAVING count(*) FILTER (WHERE f.verdict IN ('stale', 'wrong')) > 0
), backlog AS (
    SELECT e.id, e.kind, e.summary, e.author, e.scope, e.fleet_id,
           fl.first_flag, fl.last_flag, fl.helpful, fl.stale, fl.wrong,
           coalesce(au.status = 'revoked', FALSE)
             OR coalesce(lw.at < $1::timestamptz - interval '720 hours', FALSE) AS orphaned
      FROM flagged fl
      JOIN entries e ON e.id = fl.entry_id
      LEFT JOIN agents au ON au.name = e.author
      LEFT JOIN LATERAL (
          SELECT max(w.created_at) AS at FROM entries w WHERE w.author = e.author
      ) lw ON TRUE
     WHERE e.state = 'active' AND {{inner}}
)
SELECT * FROM backlog
 WHERE {{outer}}
 ORDER BY first_flag, id
 LIMIT ${{limit}} OFFSET ${{offset}}
"""

BATCH_FEEDBACK = f"""
SELECT
    entry_id,
    {_VERDICT_COUNTS}
FROM feedbacks f
WHERE entry_id = ANY($1) AND {_RATING_COUNTS}
GROUP BY entry_id
"""

# BATCH_FEEDBACK's counts plus the two dates "last confirmed" is made of (SPEC §4.2):
# the newest noted helpful row and the newest flag (also read by a pin's flagged check, ADR 0076).
BATCH_FEEDBACK_SIGNALS = f"""
SELECT g.*, corr.verdict AS corr_verdict, corr.note AS corr_note,
       corr."user" AS corr_user, corr.updated_at AS corr_updated_at
FROM (
    SELECT
        entry_id,
        {_VERDICT_COUNTS},
        MAX(updated_at) FILTER (
            WHERE verdict = 'helpful' AND coalesce(note, '') ~ '\\S'
        ) AS newest_noted_helpful_at,
        MAX(updated_at) FILTER (WHERE verdict IN ('stale', 'wrong')) AS newest_flag_at
    FROM feedbacks f
    WHERE entry_id = ANY($1) AND {_RATING_COUNTS}
    GROUP BY entry_id
) g
LEFT JOIN LATERAL (
    SELECT f.verdict, f.note, f."user", f.updated_at
      FROM feedbacks f
     WHERE f.entry_id = g.entry_id
       AND f.verdict IN ('stale', 'wrong')
       AND coalesce(f.note, '') ~ '\\S'
       AND {_RATING_COUNTS}
     ORDER BY f.updated_at DESC, f."user", f.agent
     LIMIT 1
) corr ON true
"""

# Per entry in $1, how many entries it replaced (``entries_superseded_by_idx``), counting
# only those the reader may read: ``{vis}`` is the visibility clause (ADRs 0011, 0033).
COUNT_SUPERSEDES = """
SELECT superseded_by AS entry_id, COUNT(*) AS n
FROM entries
WHERE superseded_by = ANY($1) AND superseded_by IS NOT NULL{vis}
GROUP BY superseded_by
"""

# ADR 0068: per reporter, flags written while the entry was current and those whose entry
# was superseded after. No superseded_at exists, so the successor's created_at stands in.
FLAG_COUNTS = f"""
SELECT f."user" AS reporter,
       COUNT(*) AS flags,
       COUNT(*) FILTER (WHERE s.id IS NOT NULL) AS confirmed
FROM feedbacks f
JOIN entries e ON e.id = f.entry_id
LEFT JOIN entries s ON s.id = e.superseded_by AND e.state = 'superseded'
WHERE f.verdict IN ('stale', 'wrong')
  AND (s.id IS NULL OR s.created_at > f.updated_at)
  AND {_RATING_COUNTS}
GROUP BY f."user"
"""


class FeedbackStore(PgConnections):
    async def flag_counts(self) -> list[FlagCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(FLAG_COUNTS)
        return [
            FlagCount(
                user=row["reporter"], flags=int(row["flags"]), confirmed=int(row["confirmed"])
            )
            for row in rows
        ]

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        async with self._connection() as conn:
            updated_at = await conn.fetchval(
                UPSERT_FEEDBACK,
                feedback.entry_id,
                feedback.user,
                feedback.agent,
                feedback.verdict.value,
                feedback.note,
                feedback.updated_at,
            )
        return replace(feedback, updated_at=updated_at)

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        """Upsert several feedback rows in one transaction."""
        if not feedbacks:
            return []
        async with self._transaction() as conn:
            # The transaction's start time: the stamp for rows without their own ``updated_at``.
            now = await conn.fetchval("SELECT now()")
            await conn.executemany(
                UPSERT_FEEDBACK,
                [
                    (f.entry_id, f.user, f.agent, f.verdict.value, f.note, f.updated_at)
                    for f in feedbacks
                ],
            )
        return [replace(f, updated_at=f.updated_at or now) for f in feedbacks]

    async def feedback_counts(self, entry_id: str) -> tuple[int, int, int]:
        if not is_valid_uuid(entry_id):
            return (0, 0, 0)
        async with self._connection() as conn:
            row = await conn.fetchrow(COUNT_FEEDBACK, entry_id)
        return (row["helpful"], row["stale"], row["wrong"]) if row else (0, 0, 0)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, tuple[int, int, int]]:
        """Batched counts; every valid ID is present, keyed by its canonical form."""
        ids = canonical_uuids(entry_ids)
        if not ids:
            return {}
        async with self._connection() as conn:
            rows = await conn.fetch(BATCH_FEEDBACK, ids)
        counts = dict.fromkeys(ids, (0, 0, 0))
        for r in rows:
            counts[str(r["entry_id"])] = (r["helpful"], r["stale"], r["wrong"])
        return counts

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        """Batched counts, last-confirmed dates and corrections, plus the visibility-aware
        ``supersedes`` counts when asked; every valid ID is present, keyed canonically."""
        ids = canonical_uuids(entry_ids)
        if not ids:
            return {}
        supersedes: dict[str, int] = {}
        async with self._connection() as conn:
            rows = await conn.fetch(BATCH_FEEDBACK_SIGNALS, ids)
            if count_supersedes:
                params: list[Any] = [ids]
                vis = visibility_clause(visibility, params)
                sql = COUNT_SUPERSEDES.format(vis=f" AND {vis}" if vis else "")
                supersedes = {str(r["entry_id"]): r["n"] for r in await conn.fetch(sql, *params)}
        signals = {eid: FeedbackSignals(0, 0, 0, supersedes=supersedes.get(eid, 0)) for eid in ids}
        for r in rows:
            eid = str(r["entry_id"])
            correction = (
                Correction(
                    Verdict(r["corr_verdict"]),
                    truncate_note(r["corr_note"]),
                    r["corr_user"],
                    r["corr_updated_at"],
                )
                if r["corr_updated_at"] is not None
                else None
            )
            signals[eid] = FeedbackSignals(
                r["helpful"],
                r["stale"],
                r["wrong"],
                last_confirmed(r["newest_noted_helpful_at"], r["newest_flag_at"]),
                correction,
                supersedes.get(eid, 0),
                r["newest_flag_at"],
            )
        return signals

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's newest feedback rows (the primary key narrows the scan to the entry)."""
        if not is_valid_uuid(entry_id):
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FEEDBACK, entry_id, limit)
        return [row_to_feedback(r) for r in rows]

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        canonical = {eid: canonical_uuid(eid) for eid in entry_ids}
        ids = list(dict.fromkeys(c for c in canonical.values() if c is not None))
        rows_by_id: dict[str, list[Feedback]] = {c: [] for c in ids}
        if ids:
            async with self._connection() as conn:
                rows = await conn.fetch(LIST_FEEDBACK_MANY, ids, limit)
            for r in rows:
                rows_by_id[str(r["entry_id"])].append(row_to_feedback(r))
        return {eid: rows_by_id[c] if c is not None else [] for eid, c in canonical.items()}

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries, newest first (a scan and top-N sort: admin panel only)."""
        clauses = [_RATING_COUNTS]
        params: list[Any] = []
        add = partial(add_clause, clauses, params)

        for value, fragment in (
            (filters.entry_id, "f.entry_id = ?"),
            (filters.fleet_id, "e.fleet_id = ?"),
        ):
            if value is None:
                continue
            if is_valid_uuid(value):
                add(fragment, value)
            else:
                clauses.append("FALSE")
        if filters.reporter is not None:
            add('f."user" = ?', filters.reporter)
        if filters.verdict is not None:
            add("f.verdict = ?", filters.verdict.value)
        if filters.author is not None:
            add("e.author = ?", filters.author)
        if filters.authors:
            add("e.author = ANY(?::text[])", list(filters.authors))
        vis = visibility_clause(visibility, params, "e.")
        if vis is not None:
            clauses.append(vis)
        sql = FEEDBACK_LOG.format(
            where=" AND ".join(clauses), limit=len(params) + 1, offset=len(params) + 2
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [
            FeedbackLogItem(
                feedback=row_to_feedback(r),
                entry_kind=Kind(r["kind"]),
                entry_summary=r["summary"],
                entry_author=r["author"],
                entry_scope=r["scope"],
                entry_fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                entry_state=EntryState(r["state"]),
            )
            for r in rows
        ]

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        """The flag backlog, oldest flag first; all standing flags are grouped before the page is
        cut."""
        params: list[Any] = [now]
        inner = ["TRUE"]
        outer = ["TRUE"]
        entry_filter: list[str] = []
        if filters.fleet_id is not None:
            if is_valid_uuid(filters.fleet_id):
                params.append(filters.fleet_id)
                entry_filter.append(f"fleet_id = ${len(params)}")
            else:
                inner.append("FALSE")
        if filters.authors:
            params.append(list(filters.authors))
            entry_filter.append(f"author = ANY(${len(params)}::text[])")
        inner += [f"e.{c}" for c in entry_filter]
        narrow = (
            " AND f.entry_id IN (SELECT id FROM entries WHERE " + " AND ".join(entry_filter) + ")"
            if entry_filter
            else ""
        )
        vis = visibility_clause(visibility, params, "e.")
        if vis is not None:
            inner.append(vis)
        if filters.flagged_before is not None:
            params.append(filters.flagged_before)
            outer.append(f"first_flag < ${len(params)}")
        if filters.orphaned:
            outer.append("orphaned")
        sql = FLAG_BACKLOG.format(
            narrow=narrow,
            inner=" AND ".join(inner),
            outer=" AND ".join(outer),
            limit=len(params) + 1,
            offset=len(params) + 2,
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [
            FlaggedEntry(
                entry_id=str(r["id"]),
                kind=Kind(r["kind"]),
                summary=r["summary"],
                author=r["author"],
                scope=r["scope"],
                fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                first_flag_at=r["first_flag"],
                last_flag_at=r["last_flag"],
                helpful=int(r["helpful"]),
                stale=int(r["stale"]),
                wrong=int(r["wrong"]),
                orphaned=bool(r["orphaned"]),
            )
            for r in rows
        ]
