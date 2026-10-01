-- Rollback (ADR 0020): drops the pins 0012 started recording. The pinned
-- entries themselves are untouched.
DROP TABLE IF EXISTS pins;
