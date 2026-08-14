-- ============================================================
-- MIGRATION 015: RO3 -> RO1 (Global)
-- ------------------------------------------------------------
-- RENUMBERED 2026-08-14: was originally migration_014_ro1.sql.
-- migration_014 was independently claimed by a prod hotfix
-- (migration_014_fix_win_loss_mvp_bonus.sql, applied on
-- unified_test) that landed on a different branch while this one
-- was in progress. Renumbered to 015 to avoid a slot collision --
-- no functional change from the original 014 version, this is a
-- rename only.
--
-- Converts the match pipeline from 3 rounds/match to 1 round/match
-- on the unified_region_ro1 branch. Applied after migration_013
-- (region_leaderboard rank-band fix) -- confirmed as highest live
-- migration on unified-region-test before this branch forked.
--
-- No match_format column. Global is going fully RO1 with no
-- parallel RO3 queue on this Supabase project, so the row-count
-- assertion below is a flat literal (10), not format-aware. If a
-- mixed-format need ever comes up later, that's a separate additive
-- migration at that point -- skipping it now doesn't foreclose it.
--
-- round_number is intentionally left unconstrained-widened (still
-- `in (1,2,3)` from earlier migrations) -- new matches will only
-- ever write round 1, but tightening the constraint to =1 buys
-- nothing and risks friction with historical RO3 rows already in
-- these tables. Do not add a narrower constraint here.
-- ============================================================

-- Replace approve_ro3_match's logic with a 10-row assertion. Same
-- 200-point rank bands, same peak_rank/peak_mmr logic, same
-- matches.status/approved_by/approved_at commit -- copied verbatim
-- from the live migration_012 version, only the row-count literal
-- changes (30 -> 10).
create or replace function approve_match(p_match_id bigint, p_approved_by bigint)
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
    if (select count(*) from match_round_results where match_id = p_match_id) <> 10 then
        raise exception 'match % must have exactly 10 round-result rows', p_match_id;
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
            -- 200-point bands, carried over EXACTLY from migration_012 --
            -- do not reintroduce the older 150-point thresholds here.
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

-- Keep the old name as a callable alias during rollout -- safety net
-- in case a call site in match.py/db.py is missed during the rename
-- pass. Delegates straight through to approve_match, no duplicated
-- logic to drift out of sync.
create or replace function approve_ro3_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language sql as $$
    select * from approve_match(p_match_id, p_approved_by);
$$;
