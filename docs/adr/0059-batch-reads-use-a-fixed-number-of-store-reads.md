# Batch reads use a fixed number of store reads

## Context

ADR 0055 §5 read a batch's entries in one store call, then the feedback
and the "see also" links (ADR 0057) one entry at a time, all at once. For
10 entries that is about 30 concurrent store calls, each holding a pooled
connection, so a single `hive_get` / `POST /v1/entries/get` call took a
pod's whole pool (10 connections by default). Measured on Postgres with
10 linked entries: one call peaked at 10 connections, and 50 concurrent
calls took about 500 ms, queueing every other request behind them.

## Decision

Amends ADR 0055 §5. A batch read makes the same few store reads however
many ids it names: one for the entries, two for the feedback (the counts,
already batched for search, and the newest rows per entry,
`Store.list_feedback_many`), and two for the links (`Store.entry_links_many`,
then one read of the linked entries). The answer is unchanged: each entry
carries the feedback and links a single read returns.

## Consequences

- A batch read holds at most three pooled connections at once, not one
  per entry per field. With the same 10 entries, 50 concurrent calls
  went from about 500 ms to about 90 ms, and one call from 22 ms to 4 ms.
- The `Store` port has two more methods, mirrored in `MemoryStore`. The
  single-entry reads keep their own methods.
