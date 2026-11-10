"""SQL shared by the key check and the store for successions (ADR 0113): everything that hands down
to a name, transitively. ``UNION`` (not ``UNION ALL``) drops repeats, so a cycle ends the walk."""

from __future__ import annotations

# The agents handing down to ``$1`` as a text array (``'{}'`` when none).
INHERITED_FROM = """
(WITH RECURSIVE inh(name) AS (
    SELECT s.retired_agent FROM successions s WHERE s.successor = $1
    UNION
    SELECT s.retired_agent FROM successions s JOIN inh ON s.successor = inh.name
 ) SELECT coalesce(array_agg(name ORDER BY name), '{}'::text[]) FROM inh)
"""

# The same for the key check, correlated to the joined agent row ``a``: one round trip.
INHERITED_FROM_AGENT_ROW = """
(SELECT coalesce(array_agg(i.name ORDER BY i.name), '{}'::text[]) FROM (
    WITH RECURSIVE inh(name) AS (
        SELECT s.retired_agent FROM successions s WHERE s.successor = a.name
        UNION
        SELECT s.retired_agent FROM successions s JOIN inh ON s.successor = inh.name
    ) SELECT name FROM inh) i)
"""
