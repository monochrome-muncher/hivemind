-- ADR 0058: pinned entries, a fleet's short briefing. One row per pinned
-- (fleet, entry); a privileged agent of the fleet or an admin pins, and
-- the service caps a fleet at a few pins. `fleet_id` is text, so a
-- caller-supplied fleet id that is not a uuid simply has no pins. There
-- are no FKs, so tooling that truncates `entries` (older releases' test
-- suites, ADR 0020's backward-compat check) keeps working.
--
-- ADR 0020 expand-and-contract: a new table is additive. An older sibling
-- pod never reads it.
CREATE TABLE IF NOT EXISTS pins (
    fleet_id  text        NOT NULL,
    entry_id  uuid        NOT NULL,
    pinned_by text        NOT NULL,
    pinned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fleet_id, entry_id)
);
