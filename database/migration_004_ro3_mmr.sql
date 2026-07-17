-- Run after migrations 002 and ro3_verification.  Additive RO3 MMR storage
-- plus the single transactional approval operation used by match.py.

create table if not exists match_round_results (
    id bigserial primary key,
    match_id bigint not null references matches(id) on delete cascade,
    round_number integer not null check (round_number in (1, 2, 3)),
    player_id bigint not null references players(id),
    position integer not null check (position between 1 and 5),
    is_mvp boolean not null default false,
    mmr_delta integer not null,
    team text not null check (team in ('A', 'B')),
    unique (match_id, round_number, player_id)
);

create index if not exists idx_match_round_results_match on match_round_results(match_id);
create index if not exists idx_match_round_results_player on match_round_results(player_id);

create or replace function approve_ro3_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language plpgsql
as $$
declare
    v_match matches%rowtype;
begin
    select * into v_match from matches where id = p_match_id for update;
    if not found then
        raise exception 'match % does not exist', p_match_id;
    end if;
    if v_match.status <> 'pending_verification' then
        raise exception 'match % is not pending verification', p_match_id;
    end if;
    if (select count(*) from match_round_results where match_id = p_match_id) <> 30 then
        raise exception 'match % must have exactly 30 round-result rows', p_match_id;
    end if;

    return query
    with deltas as (
        select mrr.player_id, sum(mrr.mmr_delta)::integer as total_delta
        from match_round_results mrr
        where mrr.match_id = p_match_id
        group by mrr.player_id
    ), updated_players as (
        update players p
        set mmr = greatest(0, p.mmr + d.total_delta),
            peak_mmr = greatest(p.peak_mmr, greatest(0, p.mmr + d.total_delta)),
            current_rank = case
                when greatest(0, p.mmr + d.total_delta) >= 1000 then 'Titans'
                when greatest(0, p.mmr + d.total_delta) >= 900 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 800 then 'Legendary1'
                when greatest(0, p.mmr + d.total_delta) >= 700 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 600 then 'Grandmaster1'
                when greatest(0, p.mmr + d.total_delta) >= 500 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 400 then 'Master1'
                when greatest(0, p.mmr + d.total_delta) >= 300 then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 200 then 'PRO1'
                when greatest(0, p.mmr + d.total_delta) >= 100 then 'Elite2'
                else 'Elite1'
            end,
            current_division = '',
            peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
                when greatest(0, p.mmr + d.total_delta) >= 1000 then 'Titans' when greatest(0, p.mmr + d.total_delta) >= 900 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 800 then 'Legendary1' when greatest(0, p.mmr + d.total_delta) >= 700 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 600 then 'Grandmaster1' when greatest(0, p.mmr + d.total_delta) >= 500 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 400 then 'Master1' when greatest(0, p.mmr + d.total_delta) >= 300 then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 200 then 'PRO1' when greatest(0, p.mmr + d.total_delta) >= 100 then 'Elite2' else 'Elite1' end
                else p.peak_rank end,
            updated_at = now()
        from deltas d
        where p.id = d.player_id
        returning p.id, p.mmr - d.total_delta as before_mmr, p.mmr as after_mmr, d.total_delta
    ), updated_match_players as (
        update match_players mp
        set mmr_before = up.before_mmr, mmr_after = up.after_mmr, mmr_change = up.total_delta
        from updated_players up
        where mp.match_id = p_match_id and mp.player_id = up.id
        returning up.id, up.before_mmr, up.after_mmr, up.total_delta
    )
    select * from updated_match_players;

    update matches
    set status = 'completed', completed_at = now(), approved_by = p_approved_by, approved_at = now()
    where id = p_match_id;
end;
$$;
