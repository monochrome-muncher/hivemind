-- Rollback (ADR 0020): drops the "see also" links 0011 started recording.
-- The entries themselves are untouched.
DROP TABLE IF EXISTS entry_links;
