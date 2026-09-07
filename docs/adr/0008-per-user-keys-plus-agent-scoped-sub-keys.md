# Per-user keys plus agent-scoped sub-keys, no OAuth in v1

The operator issues two credential kinds: a **user key** (authenticates the author; the agent self-reports its own instance ID) and an **agent-scoped sub-key** (binds author + agent instance together, so provenance is verified by the server, not self-reported). An operator/admin key can withdraw any entry. No SSO/OAuth in v1.

Considered options: OAuth/SSO federation (the "proper" enterprise path); pure self-report (the agent claims who it is and for whom it acts). Rejected: OAuth is a heavy integration surface for a self-hosted v1; pure self-report makes provenance untrustworthy, which defeats the system's purpose (agents must be able to trust that entry X really came from analyst A's agent). Sub-keys give the operator per-agent attribution and revocation without an IdP.

Consequences: credential management is operator-driven (keys issued by hand, or via an admin endpoint). SSO/OAuth federation is a v2+ extension for orgs with an existing IdP.