"""The SQL both the key check and the store share for successions (ADR 0113).

``successions (retired_agent, successor, by, at)``: ``retired_agent`` hands
its memories to ``successor``. The agents a name owns besides itself are
everything that hands down to it, transitively (chains allowed). ``UNION``
(not ``UNION ALL``) drops repeats, so even a cycle that slipped in ends the
walk instead of looping.
"""

from __future__ import annotations

# The agents that hand down to ``$1`` (not ``$1`` itself unless a cycle
# says so), as a text array: ``'{}'`` when none.
INHERITED_FROM = """
(WITH RECURSIVE inh(name) AS (
    SELECT s.retired_agent FROM successions s WHERE s.successor = $1
    UNION
    SELECT s.retired_agent FROM successions s JOIN inh ON s.successor = inh.name
 ) SELECT coalesce(array_agg(name ORDER BY name), '{}'::text[]) FROM inh)
"""

# The same, for the key check: correlated to the agent row ``a`` the key
# joins, so the key and its ownership resolve in one round trip.
INHERITED_FROM_AGENT_ROW = """
(SELECT coalesce(array_agg(i.name ORDER BY i.name), '{}'::text[]) FROM (
    WITH RECURSIVE inh(name) AS (
        SELECT s.retired_agent FROM successions s WHERE s.successor = a.name
        UNION
        SELECT s.retired_agent FROM successions s JOIN inh ON s.successor = inh.name
    ) SELECT name FROM inh) i)
"""
