-- ADR 0098: when an entry was withdrawn, so "withdrawn since you rated it
-- helpful" can be told from "already withdrawn when you rated it".
--
-- Entries already withdrawn get this migration's now(): "withdrawn by the
-- time this shipped, at the latest". A helpful rating made before that
-- lists them once, and a later one clears them.
--
-- ADR 0020 expand-and-contract: one nullable column, no default, so the
-- ALTER is a catalogue change that rewrites nothing. An older sibling pod
-- ignores the column; a withdrawal it writes during a rolling deploy leaves
-- it NULL, which the filter reads as "withdrawn after any rating". The
-- backfill touches only the withdrawn rows (a small part of the table).
SET LOCAL lock_timeout = '5s';
ALTER TABLE entries ADD COLUMN IF NOT EXISTS withdrawn_at timestamptz;
UPDATE entries SET withdrawn_at = now() WHERE state = 'withdrawn' AND withdrawn_at IS NULL;
