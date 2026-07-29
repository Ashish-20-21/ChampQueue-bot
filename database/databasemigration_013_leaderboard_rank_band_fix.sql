-- migration_013_leaderboard_rank_band_fix.sql
-- Fix 2026-07-30: region_leaderboard() was a SIXTH independent copy of
-- the rank-tier threshold table (alongside approve_ro3_match,
-- mmr_engine.derive_rank, and two chains in the ad-hoc
-- backfill_current_rank.sql) that migration_012's rank-band widen
-- (150 -> 200 point steps) missed, because it lives inside a SQL
-- function body and wasn't caught by the earlier grep sweep across
-- Python files.
--
-- Found live: every player reset to 200 MMR by migration_012 showed as
-- "Elite2" on /leaderboard instead of "Elite1", because this function's
-- CASE chain still had the old threshold (>= 151 -> Elite2) — 200 >= 151
-- is true under the old bands, even though 200 correctly maps to Elite1
-- under the new >= 201 threshold everywhere else (approve_ro3_match,
-- derive_rank, /player-stats all already showed the correct rank).
--
-- This confirms there is still no single source of truth for this
-- tier table across the codebase — see mmr_engine.derive_rank()'s
-- existing docstring warning, which applies equally here. Any future
-- band-width change needs to touch: approve_ro3_match (SQL),
-- mmr_engine.derive_rank (Python), and region_leaderboard (SQL) —
-- three places, not two.
--
-- Verified live end-to-end 2026-07-30: confirmed players.current_rank
-- was already correct (Elite1) via direct query before this fix — only
-- region_leaderboard()'s own CASE chain was stale. Ran this
-- create-or-replace on live Supabase first, re-queried
-- region_leaderboard() directly (Elite2 -> Elite1 confirmed on 5 known
-- 200-MMR rows), then verified visually via /leaderboard-refresh in
-- Discord. Re-ran the same statement on test Supabase afterward and
-- confirmed correct on 10 real rows spanning both sides of the 201
-- boundary (200 -> Elite1, 203-214 -> Elite2).

create or replace function public.region_leaderboard()
 returns table(id bigint, ign text, mmr integer, peak_mmr integer, current_rank text, wins integer, losses integer, mvp_count integer)
 language sql
 stable
as $function$
    -- current_rank is DERIVED from mmr here, not read from
    -- players.current_rank — see the big comment above approve_ro3_match
    -- for the full "why" (players.current_rank is write-only-at-approval,
    -- can go stale relative to the actual mmr, confirmed live 2026-07-19).
    select id, ign, mmr, peak_mmr,
        case
            when greatest(0, mmr) >= 2001 then 'Titans'
            when greatest(0, mmr) >= 1801 then 'Legendary2'
            when greatest(0, mmr) >= 1601 then 'Legendary1'
            when greatest(0, mmr) >= 1401 then 'Grandmaster2'
            when greatest(0, mmr) >= 1201 then 'Grandmaster1'
            when greatest(0, mmr) >= 1001 then 'Master2'
            when greatest(0, mmr) >= 801  then 'Master1'
            when greatest(0, mmr) >= 601  then 'PRO2'
            when greatest(0, mmr) >= 401  then 'PRO1'
            when greatest(0, mmr) >= 201  then 'Elite2'
            else 'Elite1'
        end as current_rank,
        wins, losses, mvp_count
    from players
    where status = 'approved'
    order by mmr desc, id asc;
$function$;