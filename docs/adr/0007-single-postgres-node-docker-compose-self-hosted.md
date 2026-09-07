# One Postgres node, docker-compose, self-hosted per org

Hivemind deploys as a single application service plus one PostgreSQL instance (with pgvector) via docker-compose. No Redis, no separate storage service, no sharding, no multi-tenant SaaS in v1. Target scale: ~50 users/agents, ~500 sessions/day, ~10k entries/day, one Postgres node.

Considered options: the Caura-style stack (Postgres + Redis + a separate storage service); a managed multi-tenant hosted offering. Rejected for v1: the target scale fits comfortably on one Postgres node; Redis buys nothing at that scale (no fan-out, no distributed locks needed for v1 semantics). Self-hosting per organization keeps deployment to one docker-compose file and sidesteps multi-tenant isolation entirely in v1.

Consequences: a second instance = a second organization (data does not flow between instances). Multi-tenant hosting and horizontal scaling are v2+ extensions, to be designed when the scale assumptions stop holding.