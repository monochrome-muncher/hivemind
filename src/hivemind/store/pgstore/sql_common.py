"""SQL fragments and WHERE-clause builders shared by several areas."""

from __future__ import annotations

from functools import partial
from typing import Any

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryFilters
from hivemind.domain.validation import is_valid_uuid

# Every ``Entry`` column except ``embedding``: no reader needs the stored
# vector, and fetching it was over half a 100-row list page's time.
# Entries read back carry ``embedding=None``.
_ENTRY_COLUMNS = """
    id, kind, summary, body, payload, sources, tags, occurred_at,
    created_at, author, agent, importance, importance_source, scope, fleet_id,
    embedding_model, state, superseded_by, withdrawn_reason, withdrawn_at, redacted_at,
    entities, entity_names, entities_model
"""


def readable_clause(visibility: Visibility | None, taken: int) -> tuple[str | None, list[Any]]:
    """The visibility condition on a candidate ``e`` and its parameters,
    numbered after the ``taken`` the statement already binds."""
    params: list[Any] = [None] * taken
    clause = visibility_clause(visibility, params, "e.")
    return clause, params[taken:]


VECTOR_SEARCH_SETTINGS = """
SET LOCAL hnsw.iterative_scan = strict_order;
SET LOCAL hnsw.ef_search = 40;
"""
"""The query-time GUCs the HNSW index (migration ``0004``) is read under.

``SET LOCAL`` in the search's transaction, not a pool ``init`` callback:
behind a transaction-mode PgBouncer (``store/pool.py``) a session ``SET``
lands on an arbitrary backend and is discarded before the search runs.
``SET LOCAL`` travels with the transaction and unsets at commit.

``iterative_scan`` (pgvector >= 0.8) keeps scanning until the filtered
``LIMIT`` is met; without it HNSW filters ``ef_search`` candidates
afterwards and a narrow-visibility reader can starve RRF's vector list.
``strict_order`` because RRF fuses on ranks (``retrieval/rrf.py``), which
``relaxed_order`` would perturb. ``ef_search = 40`` is pgvector's default,
stated explicitly; untuned for now (ADR 0025).
"""

# ADR 0067: a revoked agent's reports are left out of every feedback read
# (the rows stay, so re-activation brings them back). ``agents.name`` is
# unique, so this is an index probe; a reporter with no agent record counts.
# ADR 0111: so are the rows an admin cleanup set aside (``set_aside_by``).
# Every read of ``feedbacks`` as ``f`` uses this one condition.
_RATING_COUNTS = (
    "f.set_aside_by IS NULL AND "
    "NOT EXISTS (SELECT 1 FROM agents a WHERE a.name = f.\"user\" AND a.status = 'revoked')"
)


def filter_conditions(
    filters: EntryFilters, visibility: Visibility | None = None
) -> tuple[list[str], list[Any]]:
    """Translate ``EntryFilters`` (+ optional ``Visibility``, ADR 0011)
    into WHERE-fragments and parameters.

    Mirrors ``EntryFilters.matches`` exactly (SPEC.md §5.3). ``visibility``
    ``None`` keeps the v1 flat pool.
    """
    clauses: list[str] = []
    params: list[Any] = []
    add = partial(add_clause, clauses, params)

    if not filters.include_inactive and not filters.relied_on_changed:
        clauses.append("state = 'active'")
    if filters.kind is not None:
        add("kind = ?", filters.kind.value)
    if filters.importance_source is not None:
        add("importance_source = ?", filters.importance_source.value)
    if filters.tags:
        add("tags @> ?", list(filters.tags))
    if filters.entities:
        # Case-insensitive AND over ``entity_names`` (ADR 0016, SPEC §13).
        add("entity_names @> ?", [name.lower() for name in filters.entities])
    if filters.scope is not None:
        add("scope = ?", filters.scope)
    if filters.fleet_id is not None:
        if is_valid_uuid(filters.fleet_id):
            add("fleet_id = ?", filters.fleet_id)
        else:
            clauses.append("FALSE")  # no such fleet, so no entry (as MemoryStore)
    if filters.author is not None:
        add("author = ?", filters.author)
    if filters.authors:
        add("author = ANY(?::text[])", list(filters.authors))
    if filters.agent is not None:
        add("agent = ?", filters.agent)
    if filters.occurred_from is not None:
        add("occurred_at >= ?", filters.occurred_from)
    if filters.occurred_to is not None:
        add("occurred_at <= ?", filters.occurred_to)
    if filters.created_from is not None:
        add("created_at >= ?", filters.created_from)
    if filters.created_to is not None:
        add("created_at <= ?", filters.created_to)

    if filters.flagged:
        # ADR 0054: at least one stale or wrong report. The feedbacks primary
        # key (entry_id, "user", agent) serves the per-entry probe.
        clauses.append(
            "EXISTS (SELECT 1 FROM feedbacks f WHERE f.entry_id = entries.id"
            " AND f.verdict IN ('stale', 'wrong') AND " + _RATING_COUNTS + ")"
        )

    if filters.relied_on_changed:
        clauses.append(_relied_on_changed_clause(filters.reporter, params))

    vis_clause = visibility_clause(visibility, params)
    if vis_clause is not None:
        clauses.append(vis_clause)

    return clauses, params


def _relied_on_changed_clause(reporter: tuple[str, str] | None, params: list[Any]) -> str:
    """The ``relied_on_changed`` condition for ``reporter``; appends its parameters."""
    # ADR 0098: the reporter's helpful row, and since its updated_at the
    # entry was withdrawn or superseded by someone else, or another
    # reporter filed stale/wrong. A withdrawal with no recorded time
    # (written by a pre-0016 pod) counts.
    if reporter is None:
        return "FALSE"
    params.extend(reporter)
    user_p, agent_p = f"${len(params) - 1}", f"${len(params)}"
    return (
        "EXISTS (SELECT 1 FROM feedbacks h WHERE h.entry_id = entries.id"
        f" AND h.\"user\" = {user_p} AND h.agent = {agent_p} AND h.verdict = 'helpful'"
        " AND h.set_aside_by IS NULL"
        " AND (entries.state = 'withdrawn'"
        " AND coalesce(entries.withdrawn_at > h.updated_at, TRUE)"
        " OR entries.state = 'superseded' AND EXISTS (SELECT 1 FROM entries s"
        ' WHERE s.id = entries.superseded_by AND s.author <> h."user"'
        " AND s.created_at > h.updated_at)"
        " OR EXISTS (SELECT 1 FROM feedbacks f WHERE f.entry_id = entries.id"
        " AND f.verdict IN ('stale', 'wrong') AND f.updated_at > h.updated_at"
        ' AND f."user" <> h."user"'
        " AND " + _RATING_COUNTS + ")))"
    )


def visibility_clause(
    visibility: Visibility | None, params: list[Any], table: str = ""
) -> str | None:
    """The visibility clause for ``visibility`` (ADR 0011), or ``None``
    for no restriction; appends its parameters to ``params``.

    Mirrors ``domain.access.entry_is_visible``. ``table`` qualifies the
    ``entries`` columns (``"e."``) when the query joins another table.
    """
    if visibility is None or visibility.is_admin:
        return None
    if visibility.level == TrustLevel.UNTRUSTED:
        return "FALSE"

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if visibility.inherited:
        # ADR 0113: own = the caller's name or an agent that handed down to it.
        own = f"{table}author = ANY({_p(list(visibility.owned))}::text[])"
    else:
        own = f"{table}author = {_p(visibility.name)}"
    if visibility.level == TrustLevel.PRIVILEGED:
        # L3 reads every fleet's entries.
        return (
            f"({table}scope = 'org' OR ({table}scope = 'self' AND {own}) OR {table}scope = 'fleet')"
        )
    # L1/L2: fleet entries only if own or in the home fleet.
    home_p = _p(visibility.home_fleet_id)
    return (
        f"({table}scope = 'org' "
        f"OR ({table}scope = 'self' AND {own}) "
        f"OR ({table}scope = 'fleet' AND ({own} OR {table}fleet_id = {home_p})))"
    )


def add_clause(clauses: list[str], params: list[Any], fragment: str, value: Any) -> None:
    """Append ``fragment`` to ``clauses``, its ``?`` bound to a new parameter of ``value``."""
    params.append(value)
    clauses.append(fragment.replace("?", f"${len(params)}"))
