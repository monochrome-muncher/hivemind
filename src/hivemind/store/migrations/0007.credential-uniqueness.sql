-- ADRs 0028, 0031, 0039: "one live key per agent" and "one live org key"
-- are schema invariants, not conventions every writer must remember.
-- Two racing writers (REST activate + CLI issue-agent; two concurrent
-- org-key rotations) previously left several live keys.
--
-- Existing duplicates are resolved DETERMINISTICALLY before the indexes
-- are built, or the CREATE UNIQUE INDEX below would fail on a pool that
-- already holds them: per agent name (kind = 'agent') and for kind =
-- 'org', the NEWEST row wins (created_at, ties broken by key_hash) and
-- every older row is deleted - i.e. its key is revoked. The surviving
-- key is the one most recently handed out. The deletion cannot be
-- undone by the rollback (a revoked key is gone by design; its hash
-- is not needed anywhere).
--
-- Pre-v2 `user` rows and name-less `agent` rows are NOT touched here
-- (the partial indexes ignore them); the authenticator now rejects them
-- (ADR 0039) and an operator may delete them with SQL.
--
-- ADR 0020 expand-and-contract: adding a unique index is additive; an
-- older sibling pod that races two inserts now gets a unique violation
-- instead of silently creating a second live key.
DELETE FROM credentials c
 USING credentials newer
 WHERE c.kind = 'agent'
   AND c.agent_name IS NOT NULL
   AND newer.kind = 'agent'
   AND newer.agent_name = c.agent_name
   AND (newer.created_at, newer.key_hash) > (c.created_at, c.key_hash);

DELETE FROM credentials c
 USING credentials newer
 WHERE c.kind = 'org'
   AND newer.kind = 'org'
   AND (newer.created_at, newer.key_hash) > (c.created_at, c.key_hash);

CREATE UNIQUE INDEX credentials_one_agent_key
    ON credentials (agent_name)
    WHERE kind = 'agent' AND agent_name IS NOT NULL;

CREATE UNIQUE INDEX credentials_one_org_key
    ON credentials ((kind))
    WHERE kind = 'org';
