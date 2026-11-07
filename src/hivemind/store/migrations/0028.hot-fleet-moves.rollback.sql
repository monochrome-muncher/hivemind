-- Legal rollback (ADR 0020): an index and a storage parameter carry no
-- information that is not already in the table, so this destroys nothing. It
-- returns `entries.fleet_id` to its btree index and the table to the default
-- fillfactor of 100; moves become slow again (every moved row is re-inserted
-- into every index). Pages already written at 50 stay half empty until a
-- `VACUUM FULL entries`.
--
-- Statement order: yoyo reads this file's statements in reverse to pair them
-- with the forward steps and runs them in file order on rollback. So the
-- index comes back first and the fillfactor is reset second, the reverse of
-- the forward file. `CONCURRENTLY` for the same reason the forward direction
-- uses it, governed by the forward file's `-- transactional: false`. If a
-- cancelled run leaves an INVALID `entries_fleet_idx`, drop it: it blocks
-- HOT updates just as a valid one does.
CREATE INDEX CONCURRENTLY IF NOT EXISTS entries_fleet_idx ON entries (fleet_id);

ALTER TABLE entries RESET (fillfactor);
