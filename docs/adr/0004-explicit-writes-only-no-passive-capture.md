# Explicit writes only (no passive capture in v1)

All entries enter Hivemind through explicit writes by the agent that authored them. The agent decides what matters; the service does not ingest raw transcripts, tool-call events, or session logs in v1.

Considered options: passive capture — hook-based observation capture (agentmemory's model) and/or scheduled transcript distillation (Caura's "Interviewer"). Rejected for v1: passive capture means instrumenting every agent framework (Claude Code, pi, Codex, ...) and running LLM distillation over raw sessions, a much larger integration and data-quality surface. Explicit writes keep the write path and data quality tractable; the agent is best placed to know what matters in its own session.

Consequences: memories exist only if an agent bothers to write them. Passive capture (transcript ingestion with crash-safe dedup) and server-side distillation are documented v2+ extensions, not removed ideas.