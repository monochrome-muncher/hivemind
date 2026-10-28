"""What "my own memories" means once agents can hand theirs down (ADR 0113).

A caller that owns more than its own name (it is a successor) means all of it
when it filters by its own name as ``author``: the skill's catch-up reads
``hive_list`` with ``author`` = the agent's name and ``flagged: true`` to fix
its own flagged entries, and inherited ones belong in that list. A filter on
any other name stays literal, so an agent can still look at what the retired
agent wrote by asking for that name; an admin never expands.
"""

from __future__ import annotations

from dataclasses import replace

from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryFilters
from hivemind.domain.feedback import FeedbackLogFilters


def own_author_filter[F: (EntryFilters, FeedbackLogFilters)](
    filters: F, visibility: Visibility
) -> F:
    """``filters`` with ``author`` = the caller's own name widened to every
    author the caller owns; unchanged when it owns nothing inherited."""
    if (
        visibility.is_admin
        or not visibility.inherited
        or filters.author is None
        or filters.author != visibility.name
    ):
        return filters
    return replace(filters, author=None, authors=visibility.owned)
