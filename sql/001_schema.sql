-- brain-boot-mcp: Postgres schema.
-- Apply with: psql "$BRAIN_BOOT_PG_DSN" -f sql/001_schema.sql -f sql/002_functions.sql
-- Requires Postgres 13 or newer (built in sha256(), identity columns, make_interval).
-- Tested against Postgres 16.
-- Safe to run more than once.

create table if not exists brain_rules (
    id          bigint generated always as identity primary key,
    version     integer     not null default 1,
    scope       text        not null default '*',   -- '*' means every agent
    priority    integer     not null default 100,   -- lower sorts first
    text        text        not null,
    norm_text   text        not null,
    status      text        not null default 'active' check (status in ('active', 'retired')),
    created_at  timestamptz not null default date_trunc('second', now()),
    updated_at  timestamptz not null default date_trunc('second', now())
);
create index if not exists brain_rules_active_idx on brain_rules (status, scope, priority);
create index if not exists brain_rules_norm_idx on brain_rules (norm_text) where status = 'active';
create index if not exists brain_rules_fts_idx on brain_rules using gin (to_tsvector('simple', text));

create table if not exists brain_corrections (
    id          bigint generated always as identity primary key,
    agent_key   text        not null default '*',
    text        text        not null,
    source      text        not null default 'human',
    created_at  timestamptz not null default date_trunc('second', now())
);
create index if not exists brain_corrections_recent_idx on brain_corrections (created_at desc);
create index if not exists brain_corrections_fts_idx
    on brain_corrections using gin (to_tsvector('simple', text));

create table if not exists brain_due_items (
    id          bigint generated always as identity primary key,
    agent_key   text        not null default '*',
    title       text        not null,
    due_at      timestamptz not null,
    done_at     timestamptz,
    created_at  timestamptz not null default date_trunc('second', now())
);
create index if not exists brain_due_items_open_idx on brain_due_items (due_at) where done_at is null;

create table if not exists brain_memories (
    id          bigint generated always as identity primary key,
    topic       text        not null default '',
    text        text        not null,
    created_at  timestamptz not null default date_trunc('second', now())
);
create index if not exists brain_memories_fts_idx
    on brain_memories using gin (to_tsvector('simple', text));

-- One row every time a pack is handed to an agent. lines is the exact list
-- of {key, kind, text} the pack id was computed from, so a later pack_check
-- can say which lines changed.
create table if not exists brain_boot_receipts (
    id          bigint generated always as identity primary key,
    pack_id     text        not null,
    agent_key   text        not null,
    source      text        not null check (source in ('boot', 'pack_check')),
    created_at  timestamptz not null default date_trunc('second', now()),
    lines       jsonb       not null
);
create index if not exists brain_boot_receipts_pack_idx
    on brain_boot_receipts (pack_id, agent_key, id desc);

create table if not exists brain_rule_proposals (
    id          bigint generated always as identity primary key,
    agent_key   text        not null,
    text        text        not null,
    norm_text   text        not null,
    rationale   text        not null default '',
    scope       text        not null default '*',
    status      text        not null default 'pending'
                check (status in ('pending', 'approved', 'rejected')),
    created_at  timestamptz not null default date_trunc('second', now()),
    resolved_at timestamptz,
    rule_id     bigint references brain_rules (id)
);
create index if not exists brain_rule_proposals_pending_idx
    on brain_rule_proposals (norm_text) where status = 'pending';

-- Everything recall can search, in one shape.
create or replace view brain_search_docs as
    select 'memory'::text as kind, id as ref_id, text as body, created_at from brain_memories
    union all
    select 'correction', id, text, created_at from brain_corrections
    union all
    select 'rule', id, text, updated_at from brain_rules where status = 'active';
