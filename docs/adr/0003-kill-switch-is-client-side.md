# The kill switch is client-side

"Turning Hivemind off for a session" (e.g., while spitballing or doing non-analyst work) is a client-side act: the agent's Hivemind integration (its MCP server registration/config) is disabled for that session, so the agent neither writes to nor reads from the pool. The server has no session registry in v1 and is unaware of off sessions.

Considered options: server-side session registration with server-enforced on/off; server-side private staging (session writes go to a private scratch area the author can later publish to the pool). Rejected for v1: the failure mode being solved is pool pollution from non-analyst sessions, which a hard client-side off achieves with zero server concepts. Private staging is a v2 extension (it quietly resurrects the personal-space concept deferred in ADR 0002).

Consequences: "off" sessions leave no server-side trace. If an org later needs auditability of off-sessions, that is a server-side session concept to be designed then — a spec-level note, not a v1 mechanism.