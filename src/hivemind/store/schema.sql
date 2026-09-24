-- GENERATED FILE — DO NOT EDIT, AND DO NOT APPLY.
--
-- A readable snapshot of the schema the migration chain produces
-- (ADR 0020). The source of truth is src/hivemind/store/migrations/;
-- this file exists only so the whole storage model can be read and
-- diffed in one place. Regenerate with:
--
--     make schema-ref
--
-- yoyo's own bookkeeping tables (_yoyo_migration, _yoyo_log,
-- _yoyo_version, yoyo_lock) are excluded: they are the migration
-- mechanism, not the domain schema.

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public;

COMMENT ON EXTENSION btree_gin IS 'support for indexing common datatypes in GIN';

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

CREATE FUNCTION public.entries_search_tsv_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        NEW.search_tsv :=
            setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.body, '')), 'B');
        RETURN NEW;
    END;
    $$;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.agents (
    name text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    trust_level integer DEFAULT 0 NOT NULL,
    home_fleet_id uuid,
    owner_alias text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    CONSTRAINT agents_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'active'::text, 'revoked'::text]))),
    CONSTRAINT agents_trust_level_check CHECK (((trust_level >= 0) AND (trust_level <= 3)))
);

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_kind text NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    target text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT audit_log_action_check CHECK ((action = ANY (ARRAY['agent.activate'::text, 'agent.trust_level_set'::text, 'agent.home_fleet_set'::text, 'agent.revoke'::text, 'fleet.create'::text, 'org_key.rotate'::text, 'entry.withdraw'::text, 'admin_key.issue'::text, 'admin_key.revoke'::text, 'agent_key.issue'::text]))),
    CONSTRAINT audit_log_actor_kind_check CHECK ((actor_kind = ANY (ARRAY['admin_key'::text, 'cli'::text])))
);

CREATE TABLE public.credentials (
    key_hash text NOT NULL,
    kind text NOT NULL,
    user_id text NOT NULL,
    agent_id text,
    agent_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT credentials_kind_check CHECK ((kind = ANY (ARRAY['org'::text, 'user'::text, 'agent'::text, 'admin'::text])))
);

CREATE TABLE public.entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    summary text NOT NULL,
    body text,
    payload jsonb,
    sources jsonb DEFAULT '[]'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    author text NOT NULL,
    agent text NOT NULL,
    importance integer DEFAULT 3 NOT NULL,
    scope text DEFAULT 'org'::text NOT NULL,
    embedding public.vector(1024),
    embedding_model text,
    state text DEFAULT 'active'::text NOT NULL,
    superseded_by uuid,
    withdrawn_reason text,
    search_tsv tsvector,
    fleet_id uuid,
    entities jsonb DEFAULT '[]'::jsonb NOT NULL,
    entity_names text[] DEFAULT '{}'::text[] NOT NULL,
    entities_model text,
    importance_source text DEFAULT 'default'::text NOT NULL,
    CONSTRAINT entries_importance_check CHECK (((importance >= 1) AND (importance <= 5))),
    CONSTRAINT entries_importance_source_check CHECK ((importance_source = ANY (ARRAY['caller'::text, 'default'::text]))),
    CONSTRAINT entries_kind_check CHECK ((kind = ANY (ARRAY['fact'::text, 'insight'::text, 'decision'::text]))),
    CONSTRAINT entries_state_check CHECK ((state = ANY (ARRAY['active'::text, 'superseded'::text, 'withdrawn'::text])))
);

CREATE TABLE public.feedbacks (
    entry_id uuid NOT NULL,
    "user" text NOT NULL,
    agent text NOT NULL,
    verdict text NOT NULL,
    note text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feedbacks_verdict_check CHECK ((verdict = ANY (ARRAY['helpful'::text, 'stale'::text, 'wrong'::text])))
);

CREATE TABLE public.fleets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.credentials
    ADD CONSTRAINT credentials_pkey PRIMARY KEY (key_hash);

ALTER TABLE ONLY public.entries
    ADD CONSTRAINT entries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feedbacks
    ADD CONSTRAINT feedbacks_pkey PRIMARY KEY (entry_id, "user", agent);

ALTER TABLE ONLY public.fleets
    ADD CONSTRAINT fleets_name_key UNIQUE (name);

ALTER TABLE ONLY public.fleets
    ADD CONSTRAINT fleets_pkey PRIMARY KEY (id);

CREATE INDEX agents_fleet_idx ON public.agents USING btree (home_fleet_id);

CREATE INDEX audit_log_actor_idx ON public.audit_log USING btree (actor);

CREATE INDEX audit_log_occurred_at_idx ON public.audit_log USING btree (occurred_at);

CREATE INDEX entries_author_idx ON public.entries USING btree (author);

CREATE INDEX entries_embedding_hnsw_idx ON public.entries USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');

CREATE INDEX entries_entity_names_gin_idx ON public.entries USING gin (entity_names);

CREATE INDEX entries_fleet_idx ON public.entries USING btree (fleet_id);

CREATE INDEX entries_kind_idx ON public.entries USING btree (kind);

CREATE INDEX entries_occurred_idx ON public.entries USING btree (occurred_at);

CREATE INDEX entries_search_tsv_idx ON public.entries USING gin (search_tsv);

CREATE INDEX entries_state_idx ON public.entries USING btree (state);

CREATE INDEX entries_tags_gin_idx ON public.entries USING gin (tags);

CREATE TRIGGER entries_search_tsv_trigger BEFORE INSERT OR UPDATE OF summary, body ON public.entries FOR EACH ROW EXECUTE FUNCTION public.entries_search_tsv_update();

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_home_fleet_id_fkey FOREIGN KEY (home_fleet_id) REFERENCES public.fleets(id);

ALTER TABLE ONLY public.feedbacks
    ADD CONSTRAINT feedbacks_entry_id_fkey FOREIGN KEY (entry_id) REFERENCES public.entries(id) ON DELETE CASCADE;

