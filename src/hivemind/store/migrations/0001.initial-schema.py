"""The initial Hivemind schema (ADR 0020; formerly ``schema.sql``).

This is the head of the migration chain: everything ADRs 0002/0005/0008/
0011/0012/0016 put in the pool, as one ordered migration. Later changes
are their own numbered migrations — this file is never edited once
applied (CI pins its hash).

**Parameterised.** ``vector(<dim>)`` is a deploy-time decision (ADR 0005),
so the width comes from ``migration_context.embedding_dim``, which
``migrate`` sets before reading the chain.

**No rollback, deliberately.** ADR 0020 writes rollbacks for structural
changes only; reversing this migration drops every entry in the pool.
That direction is ``make pg-reset`` (dev) or a restore from backup
(``docs/ops-runbook.md``), never a migration.

Statements are listed individually rather than split from one blob: the
plpgsql trigger body contains ``;`` inside ``$$`` quoting, and an
explicit list needs no dollar-quote-aware parser to get that right.
"""

from yoyo import step

from hivemind.store import migration_context

__transactional__ = True

_DIM = migration_context.embedding_dim

_STATEMENTS = [
    # --- extensions -------------------------------------------------
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS btree_gin",
    # --- entries (ADR 0002: one org, one pool) ----------------------
    # CHECK constraints are NAMED so a later migration can replace one
    # by name (ADR 0020). An unnamed inline CHECK is unreachable on a
    # live pool: the CREATE TABLE that carries it is a no-op once the
    # table exists, which is how `credentials.kind` drifted under the
    # old declarative re-apply.
    f"""
    CREATE TABLE entries (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind             text NOT NULL
                         CONSTRAINT entries_kind_check
                         CHECK (kind IN ('fact', 'insight', 'decision')),
        summary          text NOT NULL,
        body             text,
        payload          jsonb,
        sources          jsonb NOT NULL DEFAULT '[]',
        tags             text[] NOT NULL DEFAULT '{{}}',
        occurred_at      timestamptz NOT NULL,
        created_at       timestamptz NOT NULL DEFAULT now(),
        author           text NOT NULL,
        agent            text NOT NULL,
        importance       int  NOT NULL DEFAULT 3
                         CONSTRAINT entries_importance_check
                         CHECK (importance BETWEEN 1 AND 5),
        scope            text NOT NULL DEFAULT 'org',
        embedding        vector({_DIM}),
        embedding_model  text,
        state            text NOT NULL DEFAULT 'active'
                         CONSTRAINT entries_state_check
                         CHECK (state IN ('active', 'superseded', 'withdrawn')),
        superseded_by    uuid,
        withdrawn_reason text,
        search_tsv       tsvector,
        -- Column ORDER matters here only for provability: it reproduces
        -- the pre-ADR-0020 physical order (these last four arrived as
        -- ALTER ... ADD COLUMN), so `pg_dump` of an old pool and of a
        -- freshly-migrated one are byte-identical.
        fleet_id         uuid,
        entities         jsonb  NOT NULL DEFAULT '[]',
        entity_names     text[] NOT NULL DEFAULT '{{}}',
        entities_model   text
    )
    """,
    # --- full-text search (SPEC §6.2 keyword stream) ----------------
    # `to_tsvector` is STABLE, not IMMUTABLE, so it cannot back a STORED
    # generated column; the canonical pattern is a plain tsvector column
    # maintained by a trigger (setweight 'A' on the summary ranks it
    # above 'B' on the body).
    """
    CREATE FUNCTION entries_search_tsv_update() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        NEW.search_tsv :=
            setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.body, '')), 'B');
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE TRIGGER entries_search_tsv_trigger
        BEFORE INSERT OR UPDATE OF summary, body ON entries
        FOR EACH ROW EXECUTE FUNCTION entries_search_tsv_update()
    """,
    # --- entries indexes --------------------------------------------
    "CREATE INDEX entries_search_tsv_idx ON entries USING GIN (search_tsv)",
    "CREATE INDEX entries_tags_gin_idx ON entries USING GIN (tags)",
    "CREATE INDEX entries_entity_names_gin_idx ON entries USING GIN (entity_names)",
    "CREATE INDEX entries_kind_idx ON entries (kind)",
    "CREATE INDEX entries_state_idx ON entries (state)",
    "CREATE INDEX entries_occurred_idx ON entries (occurred_at)",
    "CREATE INDEX entries_fleet_idx ON entries (fleet_id)",
    "CREATE INDEX entries_author_idx ON entries (author)",
    # --- feedback (SPEC §4.2): one row per (entry, user, agent) -----
    """
    CREATE TABLE feedbacks (
        entry_id   uuid NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        "user"     text NOT NULL,
        agent      text NOT NULL,
        verdict    text NOT NULL
                   CONSTRAINT feedbacks_verdict_check
                   CHECK (verdict IN ('helpful', 'stale', 'wrong')),
        note       text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (entry_id, "user", agent)
    )
    """,
    # --- credentials (ADR 0008; reinterpreted by ADR 0012) ----------
    # `key_hash` is the sha256 of the raw API key. The `user` kind is
    # retired at the code level (ADR 0012) but still permitted here:
    # this migration reproduces the pre-ADR-0020 schema EXACTLY so the
    # cutover is a provable no-op. Tightening it is a later migration
    # and its own decision (auth.py still has a `kind == "user"` read
    # branch).
    """
    CREATE TABLE credentials (
        key_hash   text PRIMARY KEY,
        kind       text NOT NULL
                   CONSTRAINT credentials_kind_check
                   CHECK (kind IN ('org', 'user', 'agent', 'admin')),
        user_id    text NOT NULL,
        agent_id   text,
        agent_name text,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # --- access control (ADRs 0011-0012) ----------------------------
    """
    CREATE TABLE fleets (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name       text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE agents (
        name          text PRIMARY KEY,
        status        text NOT NULL DEFAULT 'pending'
                      CONSTRAINT agents_status_check
                      CHECK (status IN ('pending', 'active')),
        trust_level   int  NOT NULL DEFAULT 0
                      CONSTRAINT agents_trust_level_check
                      CHECK (trust_level BETWEEN 0 AND 3),
        home_fleet_id uuid REFERENCES fleets (id),
        owner_alias   text,
        created_at    timestamptz NOT NULL DEFAULT now(),
        activated_at  timestamptz
    )
    """,
    "CREATE INDEX agents_fleet_idx ON agents (home_fleet_id)",
    # NOTE: `entries.fleet_id` deliberately carries NO foreign key to
    # `fleets` — that matches the pre-ADR-0020 schema. Adding one would
    # be a behaviour change, not a migration-mechanism change.
]

steps = [step(statement) for statement in _STATEMENTS]
