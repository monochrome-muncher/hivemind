-- Hivemind schema (idempotent). Applied by hivemind.store.migrate.migrate().
-- `:dim` is substituted with the deploy-time embedding dimension (ADR 0005).
--
-- One organization, one pool (ADR 0002): a single `entries` table with a
-- `scope` tag is the v1 seam for future narrowing.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gin;

CREATE TABLE IF NOT EXISTS entries (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text NOT NULL CHECK (kind IN ('fact', 'insight', 'decision')),
    summary       text NOT NULL,
    body          text,
    payload       jsonb,
    sources       jsonb NOT NULL DEFAULT '[]',
    tags          text[] NOT NULL DEFAULT '{}',
    occurred_at   timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    author        text NOT NULL,
    agent         text NOT NULL,
    importance    int  NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    scope         text NOT NULL DEFAULT 'org',
    embedding     vector(:dim),
    embedding_model text,
    state         text NOT NULL DEFAULT 'active'
                  CHECK (state IN ('active', 'superseded', 'withdrawn')),
    superseded_by uuid,
    withdrawn_reason text
);

-- Full-text search (SPEC §6.2 keyword stream).
--
-- `to_tsvector` is a STABLE (not IMMUTABLE) function, so it cannot back a
-- STORED generated column. The canonical Postgres pattern is a regular
-- tsvector column maintained by a trigger (setweight 'A' on the summary
-- for a stronger rank than 'B' on the body). Re-migrations rebuild the
-- trigger idempotently.
ALTER TABLE entries ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS entries_search_tsv_idx ON entries USING GIN (search_tsv);

CREATE OR REPLACE FUNCTION entries_search_tsv_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_tsv :=
        setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.body, '')), 'B');
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS entries_search_tsv_trigger ON entries;
CREATE TRIGGER entries_search_tsv_trigger
    BEFORE INSERT OR UPDATE OF summary, body ON entries
    FOR EACH ROW EXECUTE FUNCTION entries_search_tsv_update();

CREATE INDEX IF NOT EXISTS entries_tags_gin_idx   ON entries USING GIN (tags);
CREATE INDEX IF NOT EXISTS entries_kind_idx       ON entries (kind);
CREATE INDEX IF NOT EXISTS entries_state_idx      ON entries (state);
CREATE INDEX IF NOT EXISTS entries_occurred_idx   ON entries (occurred_at);

-- Feedback (SPEC §4.2): one row per (entry, user, agent), upserted.
CREATE TABLE IF NOT EXISTS feedbacks (
    entry_id   uuid NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
    "user"     text NOT NULL,
    agent      text NOT NULL,
    verdict    text NOT NULL CHECK (verdict IN ('helpful', 'stale', 'wrong')),
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_id, "user", agent)
);

-- Credentials (ADR 0008): key_hash is the sha256 of the raw API key.
CREATE TABLE IF NOT EXISTS credentials (
    key_hash   text PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('user', 'agent', 'admin')),
    user_id    text NOT NULL,
    agent_id   text,
    created_at timestamptz NOT NULL DEFAULT now()
);