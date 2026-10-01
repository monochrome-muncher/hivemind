-- ADR 0056: count searches that find nothing. One row per home fleet of
-- the searching agent ('' for callers without one), bumped once per
-- first-page search by every runner. Counts only: no query text, no agent
-- name. No FK to fleets: a counter row outliving its fleet is harmless.
--
-- ADR 0020 expand-and-contract: a new table is additive. An older sibling
-- pod never touches it.
CREATE TABLE IF NOT EXISTS search_counts (
    fleet_id text   PRIMARY KEY,
    searches bigint NOT NULL DEFAULT 0,
    empty    bigint NOT NULL DEFAULT 0,
    CONSTRAINT search_counts_searches_check CHECK (searches >= 0),
    CONSTRAINT search_counts_empty_check CHECK (empty >= 0 AND empty <= searches)
);
