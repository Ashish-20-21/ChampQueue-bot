-- migration_022_fix_achievement_grant_on_conflict.sql
-- Companion fix to migration_021 -- must be applied AFTER it (021
-- creates the partial unique indexes this function's ON CONFLICT
-- clause needs to target).
--
-- check_and_grant_achievements() (migration_020) originally read:
--   on conflict (player_id, achievement_id, season_id) do nothing
-- This targeted the old plain unique(player_id, achievement_id,
-- season_id) constraint, which migration_021 dropped (it didn't
-- actually prevent duplicates for NULL season_id rows -- see that
-- migration's comments). Postgres's ON CONFLICT requires its target to
-- match a real, currently-existing unique index/constraint; against the
-- new partial indexes, the old 3-column target no longer resolves.
--
-- check_and_grant_achievements() only ever grants PERMANENT achievements
-- (season_id is always NULL in every insert it does -- there is no
-- seasonal-achievement granting path in this function), so it only ever
-- needs to target player_achievements_unique_permanent, the partial
-- index scoped to `where season_id is null`.

create or replace function check_and_grant_achievements(p_player_id bigint)
returns table (granted_code text)
language plpgsql
as $$
declare
    v_player players%rowtype;
    v_total_kills numeric;
    v_kd numeric;
    v_rank text;
    v_code text;
begin
    select * into v_player from players where id = p_player_id;
    if not found then
        return;
    end if;

    v_total_kills := round(v_player.avg_kills * v_player.total_matches);
    v_kd := case when v_player.avg_deaths > 0 then v_player.avg_kills / v_player.avg_deaths else v_player.avg_kills end;
    v_rank := v_player.peak_rank;

    for v_code in
        select code from (values
            ('first_blood',      v_total_kills >= 250),
            ('kill_slayer',      v_total_kills >= 750),
            ('death_dealer',     v_total_kills >= 1500),
            ('sharpshooter',     v_total_kills >= 3000),
            ('mvp_king',         v_player.mvp_count >= 20),
            ('mvp_legend',       v_player.mvp_count >= 30),
            ('initiator',        v_player.total_matches >= 1),
            ('pacekeeper',       v_player.total_matches >= 10),
            ('grinder',          v_player.total_matches >= 30),
            ('veteran',          v_player.total_matches >= 50),
            ('centurion',        v_player.total_matches >= 100),
            ('positive_kd',      v_kd > 1.0),
            ('rank_pro',         v_rank in ('PRO1','PRO2','Master1','Master2','Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_master',      v_rank in ('Master1','Master2','Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_grandmaster', v_rank in ('Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_legendary',   v_rank in ('Legendary1','Legendary2','Titans')),
            ('rank_titan',       v_rank = 'Titans')
        ) as checks(code, qualifies)
        where qualifies
    loop
        insert into player_achievements (player_id, achievement_id, season_id)
        select p_player_id, a.id, null
        from achievements a
        where a.code = v_code
        -- Fixed 2026-08-21: target the partial index (player_id,
        -- achievement_id) WHERE season_id IS NULL, not the old 3-column
        -- constraint dropped by migration_021. The WHERE clause here
        -- must match the index's predicate exactly for Postgres to
        -- accept this as valid conflict-target inference.
        on conflict (player_id, achievement_id) where season_id is null do nothing
        returning v_code into granted_code;
        if granted_code is not null then
            return next;
        end if;
    end loop;
end;
$$;

-- ============================================================
-- VERIFICATION: re-run the backfill after this migration -- with the
-- fix in place, running it multiple times should now be a true no-op
-- for anyone already granted (zero new rows for already-earned badges,
-- same players still get newly-qualified ones if their stats crossed a
-- threshold since the last run).
-- ============================================================
-- select check_and_grant_achievements(id) from players where total_matches > 0;
