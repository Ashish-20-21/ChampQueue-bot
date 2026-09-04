-- ============================================================
-- MIGRATION 031: Fix approve_match() — drop stale current_division write
-- ------------------------------------------------------------
-- Bug: migration_029's approve_match() was based on migration_015's
-- original body, which still writes `current_division = ''`.
-- migration_016 dropped players.current_division entirely and
-- re-pointed approve_match() to stop writing it (see that
-- migration's STEP 4, commented block) — migration_029 never read
-- that re-pointed version and silently reintroduced the write,
-- breaking every match approval with:
--   'column "current_division" of relation "players" does not exist'
--
-- Caught live 2026-09-03 testing on ebsleroxzikxxvqblzry via a real
-- approve-button click.
--
-- Fix: re-run approve_match() with current_division removed from the
-- SET list — identical to migration_016's corrected body — with the
-- migration_029 points hook (update_season_points_for_match call)
-- still appended at the end. Nothing else changes.
-- ============================================================

create or replace function approve_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language plpgsql
as $$
declare
    v_match matches%rowtype;
    v_season_id bigint;
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

    v_season_id := v_match.season_id;

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
            -- current_division REMOVED here (migration_016) — do not
            -- re-add without also re-adding the column
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
            peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
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
                else 'Elite1' end
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

    -- Season points (migration_029) — unchanged from that migration
    if v_season_id is not null then
        perform update_season_points_for_match(p_match_id, v_season_id);
    end if;
end;
$$;

-- approve_ro3_match alias unchanged, still delegates to approve_match —
-- no re-run needed (migration_029's version stands, it's a thin wrapper).

-- Sanity check — run a real approve flow on a pending_verification
-- match, or directly:
--   select approve_match(<match_id>, <admin_discord_id>);
-- Confirm no "column current_division does not exist" error, and that
-- season_point_events picks up rows for the match as before.
