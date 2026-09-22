-- ROADMAP §4.5: record whether `importance` was caller-supplied or
-- defaulted, so "is anyone actually setting importance?" is answerable
-- (SPEC.md §4.1, §6.4). Additive-only (ADR 0020 expand-and-contract):
-- a NOT NULL column with a DEFAULT is a no-op for existing rows and
-- safe against a not-yet-upgraded sibling project reading/writing this
-- table without knowing the column exists.
ALTER TABLE entries ADD COLUMN IF NOT EXISTS importance_source text NOT NULL DEFAULT 'default';
