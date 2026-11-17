# Ratings are guarded against a racing rename

Amends [ADR 0159](0159-admin-renames-an-agent.md) decision 8.

Status: Accepted. Found in the review of the rename on 2026-11-17.

## Context

ADR 0159 guarded entry and pin writes against a rename that commits while the request is in flight, and left ratings unguarded: a rating filed under the old name was judged harmless.

It is not. The rating's key is `(entry_id, "user", agent)`, so a stray row under the old name has two effects:

- The agent's next rating of the same memory, under its new name, is a second row: one agent counts twice in the memory's quality, and a rating of its own memory slips past the own-entry rule (ADRs 0065, 0067), which compares the rater with the author.
- Renaming the agent back to the old name rewrites its ratings onto that stray row's key. Postgres refuses the update (`feedbacks_pkey`), so the rename fails with a 500 until someone deletes the row by hand; the in-memory store silently drops one of the two.

## Decision

A rating write (`hive_feedback`, `POST /v1/entries/{id}/feedback`, `POST /v1/feedback`) takes the same guard as an entry or a pin: in its transaction it key-share-locks the rater's agent row, and when the rater's name is one a rename left, it answers the retryable 503 `fleet_busy`. A rating that started first makes the rename wait for it and is then rewritten with the others. A rater that is no agent (the admin key) is not checked.

The cost is one indexed read of the agent row per rating request. Withdrawals and self-service cleanups stay unguarded, as ADR 0159 says: they file no row a later rename would collide with.

## Consequences

- A rating sent in the instant of a rename gets `fleet_busy` and succeeds on retry under the new name.
- Renaming back to a former name no longer depends on what happened during the first rename.
