# Flat org-wide pool with a scope tag

v1 has exactly one pool per organization: every agent in the org can read and write every entry. Every entry carries a `scope` field whose only v1 value is `org`. The field is a deliberate seam: narrowing audiences (team, project, channel) is a future extension that needs no schema change.

Considered options: personal + org pool with promotion (per-user private spaces, entries promoted to the org pool); scoped channels with membership rules. Rejected for v1: the use case is cross-analyst learning, which is a flat pool. Personal spaces and channels are where the design grows, but they are not a v1 commitment.

Consequences: privacy in v1 is "one organization, one trust boundary." Anything requiring per-person privacy is out of v1 scope (see SPEC.md Non-goals).