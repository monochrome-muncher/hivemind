-- ADR 0057: "see also" links between entries. A writer names up to five
-- entries it can read; each link is one row, readable from both ends
-- (the primary key serves the outgoing side, the to_id index the incoming
-- side). The insert joins on `entries`, so a link always names a stored
-- entry; there are no FKs, so tooling that truncates `entries` (the test
-- suites of older releases, ADR 0020's backward-compat check) keeps working.
--
-- ADR 0020 expand-and-contract: a new table is additive. An older sibling
-- pod never reads it, and an entry written with links reads the same on
-- it, just without them.
CREATE TABLE IF NOT EXISTS entry_links (
    from_id    uuid        NOT NULL,
    to_id      uuid        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_id, to_id),
    CONSTRAINT entry_links_not_self_check CHECK (from_id <> to_id)
);
CREATE INDEX IF NOT EXISTS entry_links_to_idx ON entry_links (to_id);
