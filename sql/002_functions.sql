-- brain-boot-mcp: Postgres functions.
-- The same behaviour as src/brain_boot_mcp/service.py, callable as plain RPC:
--
--   select brain_boot('ops_agent');
--   select brain_pack_check('pk_0123456789abcdef', 'ops_agent');
--   select * from brain_recall('invoice reminder');
--   select brain_propose_rule('ops_agent', 'Always confirm the date in writing.', 'why');
--
-- Every function takes p_now so tests can pin the clock; leave it null for now().
-- The pack id algorithm must stay identical to pack.py:
--   lines sorted by key in byte order, each as key || TAB || text || LF,
--   pack_id = 'pk_' || first 16 hex chars of sha256 over the utf8 bytes.

create or replace function brain_norm(p_text text) returns text
language sql immutable as $$
    select btrim(rtrim(lower(btrim(regexp_replace(coalesce(p_text, ''), '\s+', ' ', 'g'))), '.!'))
$$;

create or replace function brain_ts(p_ts timestamptz) returns text
language sql immutable as $$
    select to_char(p_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
$$;

-- The rows that make up a pack for one agent at one moment.
-- detail is the object shown to the agent; ord is the display order in its section.
create or replace function brain_pack_lines(
    p_agent            text,
    p_now              timestamptz,
    p_correction_days  integer default 14,
    p_correction_limit integer default 10,
    p_due_hours        integer default 24,
    p_due_limit        integer default 20
) returns table (key text, kind text, body text, ord bigint, detail jsonb)
language sql stable as $$
    select 'rule:' || r.id || ':v' || r.version,
           'rule',
           r.text,
           row_number() over (order by r.priority, r.id),
           jsonb_build_object('key', 'rule:' || r.id || ':v' || r.version,
                              'scope', r.scope, 'priority', r.priority, 'text', r.text)
      from brain_rules r
     where r.status = 'active' and r.scope in ('*', p_agent)
    union all
    select 'correction:' || c.id,
           'correction',
           c.text,
           c.ord,
           jsonb_build_object('key', 'correction:' || c.id, 'text', c.text,
                              'source', c.source, 'created_at', brain_ts(c.created_at))
      from (select x.*, row_number() over (order by x.created_at desc, x.id desc) as ord
              from brain_corrections x
             where x.agent_key in ('*', p_agent)
               and x.created_at >= p_now - make_interval(days => p_correction_days)
             order by x.created_at desc, x.id desc
             limit p_correction_limit) c
    union all
    select 'due:' || d.id,
           'due',
           brain_ts(d.due_at) || ' ' || d.title,
           d.ord,
           jsonb_build_object('key', 'due:' || d.id, 'title', d.title,
                              'due_at', brain_ts(d.due_at), 'overdue', d.due_at < p_now)
      from (select x.*, row_number() over (order by x.due_at, x.id) as ord
              from brain_due_items x
             where x.done_at is null
               and x.agent_key in ('*', p_agent)
               and x.due_at <= p_now + make_interval(hours => p_due_hours)
             order by x.due_at, x.id
             limit p_due_limit) d
$$;

-- p_lines is a jsonb array of {key, kind, text}.
create or replace function brain_pack_id_from_lines(p_lines jsonb) returns text
language sql immutable as $$
    select 'pk_' || left(encode(sha256(convert_to(coalesce(
               string_agg((e ->> 'key') || E'\t' || (e ->> 'text') || E'\n', ''
                          order by (e ->> 'key') collate "C"), ''), 'UTF8')), 'hex'), 16)
      from jsonb_array_elements(p_lines) e
$$;

create or replace function brain_check_agent(p_agent text) returns text
language plpgsql immutable as $$
declare
    v_key text := lower(btrim(coalesce(p_agent, '')));
begin
    if v_key !~ '^[a-z0-9][a-z0-9_.-]{0,62}$' then
        raise exception 'agent_key must be 1 to 63 characters of a-z, 0-9, dot, dash or underscore'
            using errcode = '22023';
    end if;
    return v_key;
end;
$$;

create or replace function brain_boot(
    p_agent            text,
    p_now              timestamptz default null,
    p_correction_days  integer default 14,
    p_correction_limit integer default 10,
    p_due_hours        integer default 24,
    p_due_limit        integer default 20
) returns jsonb
language plpgsql as $$
declare
    v_agent text := brain_check_agent(p_agent);
    v_now   timestamptz := date_trunc('second', coalesce(p_now, now()));
    v_lines jsonb;
    v_rules jsonb;
    v_corr  jsonb;
    v_due   jsonb;
    v_id    text;
begin
    select coalesce(jsonb_agg(jsonb_build_object('key', l.key, 'kind', l.kind, 'text', l.body)
                              order by l.key collate "C"), '[]'::jsonb),
           coalesce(jsonb_agg(l.detail order by l.ord) filter (where l.kind = 'rule'), '[]'::jsonb),
           coalesce(jsonb_agg(l.detail order by l.ord) filter (where l.kind = 'correction'),
                    '[]'::jsonb),
           coalesce(jsonb_agg(l.detail order by l.ord) filter (where l.kind = 'due'), '[]'::jsonb)
      into v_lines, v_rules, v_corr, v_due
      from brain_pack_lines(v_agent, v_now, p_correction_days, p_correction_limit,
                            p_due_hours, p_due_limit) l;

    v_id := brain_pack_id_from_lines(v_lines);

    insert into brain_boot_receipts (pack_id, agent_key, source, created_at, lines)
    values (v_id, v_agent, 'boot', v_now, v_lines);

    return jsonb_build_object(
        'pack_id', v_id,
        'agent_key', v_agent,
        'generated_at', brain_ts(v_now),
        'standing_rules', v_rules,
        'recent_corrections', v_corr,
        'due_items', v_due,
        'counts', jsonb_build_object(
            'standing_rules', jsonb_array_length(v_rules),
            'recent_corrections', jsonb_array_length(v_corr),
            'due_items', jsonb_array_length(v_due)),
        'instructions',
            'Follow every line in standing_rules for this whole session. '
            || 'recent_corrections are things a human told an agent to do differently; '
            || 'treat them as rules. Handle or mention due_items. '
            || 'Keep pack_id and call pack_check with it before any action that is hard to undo.'
    );
end;
$$;

create or replace function brain_pack_check(
    p_pack_id          text,
    p_agent            text,
    p_now              timestamptz default null,
    p_correction_days  integer default 14,
    p_correction_limit integer default 10,
    p_due_hours        integer default 24,
    p_due_limit        integer default 20
) returns jsonb
language plpgsql as $$
declare
    v_agent   text := brain_check_agent(p_agent);
    v_claimed text := btrim(coalesce(p_pack_id, ''));
    v_now     timestamptz := date_trunc('second', coalesce(p_now, now()));
    v_lines   jsonb;
    v_old     jsonb;
    v_id      text;
    v_added   jsonb;
    v_removed jsonb;
    v_base    jsonb;
begin
    if v_claimed !~ '^pk_[0-9a-f]{16}$' then
        raise exception 'pack_id must look like pk_ followed by 16 hex characters'
            using errcode = '22023';
    end if;

    select coalesce(jsonb_agg(jsonb_build_object('key', l.key, 'kind', l.kind, 'text', l.body)
                              order by l.key collate "C"), '[]'::jsonb)
      into v_lines
      from brain_pack_lines(v_agent, v_now, p_correction_days, p_correction_limit,
                            p_due_hours, p_due_limit) l;
    v_id := brain_pack_id_from_lines(v_lines);

    v_base := jsonb_build_object(
        'pack_id', v_claimed, 'current_pack_id', v_id, 'agent_key', v_agent,
        'checked_at', brain_ts(v_now), 'added', '[]'::jsonb, 'removed', '[]'::jsonb);

    if v_claimed = v_id then
        return v_base || jsonb_build_object(
            'status', 'UNCHANGED',
            'instruction', 'Nothing changed since this pack was issued. Carry on.');
    end if;

    -- The agent is about to learn the current pack id, so record the handover.
    insert into brain_boot_receipts (pack_id, agent_key, source, created_at, lines)
    values (v_id, v_agent, 'pack_check', v_now, v_lines);

    select r.lines into v_old
      from brain_boot_receipts r
     where r.pack_id = v_claimed and r.agent_key = v_agent
     order by r.id desc
     limit 1;

    if v_old is null then
        return v_base || jsonb_build_object(
            'status', 'UNKNOWN_PACK',
            'instruction', 'No receipt exists for this pack id and agent. Call boot again and '
                || 'follow the pack it returns before doing anything else.');
    end if;

    select coalesce(jsonb_agg(n order by (n ->> 'key') collate "C"), '[]'::jsonb)
      into v_added
      from jsonb_array_elements(v_lines) n
     where not exists (select 1 from jsonb_array_elements(v_old) o
                        where o ->> 'key' = n ->> 'key' and o ->> 'text' = n ->> 'text');

    select coalesce(jsonb_agg(o order by (o ->> 'key') collate "C"), '[]'::jsonb)
      into v_removed
      from jsonb_array_elements(v_old) o
     where not exists (select 1 from jsonb_array_elements(v_lines) n
                        where o ->> 'key' = n ->> 'key' and o ->> 'text' = n ->> 'text');

    return v_base || jsonb_build_object(
        'status', 'CHANGED',
        'added', v_added,
        'removed', v_removed,
        'instruction', 'Your pack is out of date. Follow the added lines first, stop following '
            || 'the removed lines, then use current_pack_id for later checks.');
end;
$$;

-- OR match over lowercase alphanumeric words, best rank first.
create or replace function brain_recall(
    p_query text,
    p_limit integer default 5,
    p_kinds text[] default null
) returns table (kind text, ref_id bigint, body text, score real, created_at timestamptz)
language sql stable as $$
    with q as (
        select to_tsquery('simple', string_agg(distinct t, ' | ')) as tsq
          from regexp_split_to_table(lower(coalesce(p_query, '')), '[^a-z0-9]+') t
         where length(t) >= 2
    )
    select d.kind, d.ref_id, d.body,
           ts_rank(to_tsvector('simple', d.body), q.tsq) as score,
           d.created_at
      from brain_search_docs d, q
     where q.tsq is not null
       and to_tsvector('simple', d.body) @@ q.tsq
       and (p_kinds is null or d.kind = any (p_kinds))
     order by score desc, d.created_at desc, d.ref_id
     limit greatest(1, least(coalesce(p_limit, 5), 25))
$$;

-- An agent may ask for a rule. It stays a proposal until a human approves it.
create or replace function brain_propose_rule(
    p_agent     text,
    p_text      text,
    p_rationale text default '',
    p_scope     text default '*',
    p_now       timestamptz default null
) returns jsonb
language plpgsql as $$
declare
    v_agent text := brain_check_agent(p_agent);
    v_body  text := btrim(regexp_replace(coalesce(p_text, ''), '\s+', ' ', 'g'));
    v_why   text := btrim(coalesce(p_rationale, ''));
    v_scope text;
    v_norm  text;
    v_rule  brain_rules%rowtype;
    v_id    bigint;
begin
    if v_body = '' then
        raise exception 'rule text is empty' using errcode = '22023';
    end if;
    if length(v_body) > 500 then
        raise exception 'rule text is longer than 500 characters' using errcode = '22023';
    end if;
    if length(v_why) > 1000 then
        raise exception 'rationale is longer than 1000 characters' using errcode = '22023';
    end if;
    v_scope := case when coalesce(p_scope, '') in ('', '*') then '*'
                    else brain_check_agent(p_scope) end;
    v_norm := brain_norm(v_body);

    select * into v_rule from brain_rules
     where status = 'active' and norm_text = v_norm order by id limit 1;
    if found then
        return jsonb_build_object(
            'status', 'duplicate_of_rule',
            'rule_key', 'rule:' || v_rule.id || ':v' || v_rule.version,
            'proposal_id', null,
            'message', 'This is already a standing rule. Nothing was recorded.');
    end if;

    select id into v_id from brain_rule_proposals
     where status = 'pending' and norm_text = v_norm order by id limit 1;
    if found then
        return jsonb_build_object(
            'status', 'already_pending', 'rule_key', null, 'proposal_id', v_id,
            'message', 'The same proposal is already waiting for a human decision.');
    end if;

    insert into brain_rule_proposals (agent_key, text, norm_text, rationale, scope, created_at)
    values (v_agent, v_body, v_norm, v_why, v_scope,
            date_trunc('second', coalesce(p_now, now())))
    returning id into v_id;

    return jsonb_build_object(
        'status', 'pending', 'rule_key', null, 'proposal_id', v_id,
        'message', 'Recorded as a proposal. It is not a rule and will not appear in any '
            || 'pack until a human approves it.');
end;
$$;

-- Human side. Do not grant this to the role your agents connect as.
create or replace function brain_approve_proposal(
    p_proposal_id bigint,
    p_priority    integer default 100,
    p_now         timestamptz default null
) returns bigint
language plpgsql as $$
declare
    v_now  timestamptz := date_trunc('second', coalesce(p_now, now()));
    v_prop brain_rule_proposals%rowtype;
    v_rule bigint;
begin
    select * into v_prop from brain_rule_proposals where id = p_proposal_id for update;
    if not found then
        raise exception 'proposal % does not exist', p_proposal_id using errcode = '22023';
    end if;
    if v_prop.status <> 'pending' then
        raise exception 'proposal % is already %', p_proposal_id, v_prop.status
            using errcode = '22023';
    end if;
    insert into brain_rules (scope, priority, text, norm_text, created_at, updated_at)
    values (v_prop.scope, p_priority, v_prop.text, v_prop.norm_text, v_now, v_now)
    returning id into v_rule;
    update brain_rule_proposals
       set status = 'approved', resolved_at = v_now, rule_id = v_rule
     where id = p_proposal_id;
    return v_rule;
end;
$$;
