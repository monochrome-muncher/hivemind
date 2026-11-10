"""What "my own memories" means once agents can hand theirs down (ADR 0113).

A successor filtering by its own name as ``author`` means every author it owns (the skill's
catch-up of flagged entries relies on it). Any other name stays literal; an admin never expands.
"""

from __future__ import annotations

from dataclasses import replace

from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryFilters
from hivemind.domain.feedback import FeedbackLogFilters


def own_author_filter[F: (EntryFilters, FeedbackLogFilters)](
    filters: F, visibility: Visibility
) -> F:
    """``filters`` with the caller's own ``author`` widened to every author it owns."""
    if (
        visibility.is_admin
        or not visibility.inherited
        or filters.author is None
        or filters.author != visibility.name
    ):
        return filters
    return replace(filters, author=None, authors=visibility.owned)
