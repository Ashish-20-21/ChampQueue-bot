-- migration_012_rank_band_widen_and_global_reset.sql
-- Reform 2026-07-30: widen rank bands from 150-point to 200-point steps
-- (Elite1 0-150 -> 0-200, ... Titans 1501+ -> 2001+), and reset every
-- player's MMR to 200 as part of the ChampQueue esports -> global
-- transition (discussed and confirmed by the user this session — a
-- clean-slate reset avoids confusion between the old and new tier
-- systems, rather than trying to remap existing MMR values across two
-- different band widths).
--
-- MUST be applied together, same deploy, in this order:
--   1. This migration (function replace + defaults + one-time reset)
--   2. services/mmr_engine.py's derive_rank() updated in the same commit
--      (see that function's own docstring warning — there is no single
--      source of truth between the SQL and Python copies of this tier
--      table; approve_ro3_match is what actually writes
--      current_rank/peak_rank, derive_rank is used for /rank-progress
--      display only, but a mismatch between the two would show a
--      different rank in different commands for the same player)
--   3. services/matchmaking.py line ~45 and cogs/queue.py line ~158's
--      fallback default (player.get("mmr", 150) -> 200) updated in the
--      same commit — these are matchmaking-time fallbacks used only if
--      a player row is somehow missing an mmr value; keeping them in
--      sync with the real default avoids a silent 150-vs-200 mismatch
--      for that edge case.

-- 1. approve_ro3_match: the only function allowed to write
--    players.current_rank/peak_rank on a real match approval. Same
--    function body as before, only the threshold literals changed.
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
                when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans'
                when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1'
                when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1'
                when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1'
                when greatest(0, p.mmr + d.total_delta) >= 601  then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1'
                when greatest(0, p.mmr + d.total_delta) >= 201  then 'Elite2'
                else 'Elite1'
            end,
            current_division = '',
            peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
                when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans' when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1' when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1' when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1' when greatest(0, p.mmr + d.total_delta) >= 601 then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1' when greatest(0, p.mmr + d.total_delta) >= 201 then 'Elite2' else 'Elite1' end
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

-- 2. New-registration defaults: 150 -> 200.
alter table players alter column mmr set default 200;
alter table players alter column peak_mmr set default 200;

-- 3. One-time global reset: every existing player moves to 200 MMR /
--    Elite1, matching the esports -> global transition decision. This
--    is a deliberate clean-slate reset, not a remap of old MMR values
--    into the new band widths — confirmed with the user this is the
--    intended behavior, not an oversight.
update players
set mmr = 200,
    peak_mmr = 200,
    current_rank = 'Elite1',
    current_division = '',
    peak_rank = 'Elite1',
    updated_at = now();
