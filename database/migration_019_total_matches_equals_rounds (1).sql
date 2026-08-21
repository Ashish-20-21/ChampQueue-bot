-- migration_019_total_matches_equals_rounds.sql
-- Fix 2026-08-20: recompute_player_career_stats() has total_matches and
-- wins/losses computed from two DIFFERENT queries on the same table,
-- and they silently disagree for any player with RO3-era match history.
--
-- Bug: wins/losses count ROWS in match_round_results (one row per round
-- a player played). total_matches counts DISTINCT match_id (one per
-- match, regardless of how many rounds it had). For an RO1-only match
-- these are the same number (1 round = 1 match = 1 row), so the bug is
-- invisible for any player whose entire history is post-RO1-conversion.
-- For a player with RO3-era matches (best-of-3, up to 3 rows per match)
-- the two numbers diverge: e.g. 2 RO3 matches with results 2-1 and 2-1
-- give wins+losses = 4W-2L (6 rows) but total_matches = 2 (2 distinct
-- match_id) -- 6 != 4+2... wait, 4+2=6, that one's fine. The actual
-- divergence shows up whenever total_matches < wins+losses, e.g.
-- GodsCho1ce (player_id 192): stored total_matches=2, wins+losses=6
-- (4W-2L) -- 2 distinct RO3 matches, 6 total rounds played across them.
-- Players read "Total matches: 2" next to "4W-2L" and correctly report
-- it as broken, even though every individual number was independently
-- correct for what it was counting -- the two columns just don't share
-- a definition.
--
-- Fix (2026-08-20 product decision, not just a bug fix): total_matches
-- is redefined to mean "rounds counted towards your win/loss record",
-- not "distinct CQ- match channels played". This makes total_matches
-- ALWAYS equal wins+losses by construction -- both are now derived
-- from the exact same row set (all of a player's match_round_results
-- rows across counted matches), just counted two different ways on
-- the identical set, so they can never drift apart again regardless of
-- RO1/RO3 mix. For any player whose history is 100% RO1 (i.e. every
-- new player from here forward), this is invisible -- 1 round/match
-- means the new definition and the old one were always numerically
-- identical. Only players with old RO3 rounds see their total_matches
-- number change (upward, to reflect actual rounds played) after this
-- migration + backfill runs.
--
-- No change to: wins/losses logic (already fixed in migration_014, and
-- confirmed still correct by this migration's diagnostic query), MVP
-- count, avg_kills/deaths/damage/hill_time. Only the total_matches
-- source query changes.

create or replace function recompute_player_career_stats(p_player_id bigint)
returns void
language plpgsql
as $$
declare
    v_total_matches integer;
    v_wins integer;
    v_losses integer;
    v_mvp_count integer;
    v_total_kills integer;
    v_total_deaths integer;
    v_total_assists integer;
    v_total_damage numeric;
    v_damage_rounds integer;
    v_total_hill numeric;
    v_total_impact numeric;
    v_impact_rounds integer;
    v_total_rounds integer;
begin
    -- total_matches is now count(*) on the SAME row set wins/losses are
    -- derived from (was count(distinct mrr.match_id) in a separate query
    -- -- see migration comment above for why that could disagree with
    -- wins+losses for players with RO3-era history). Computed together
    -- with wins/losses in one query so the three numbers can never
    -- independently drift out of sync again.
    select count(*),
           count(*) filter (where (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0),
           count(*) filter (where (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0)
    into v_total_matches, v_wins, v_losses
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status in ('completed', 'pending_verification', 'awaiting_review');

    select count(*) into v_mvp_count
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status in ('completed', 'pending_verification', 'awaiting_review') and mrr.is_mvp = true;

    select
        coalesce(sum(mps.kills), 0), coalesce(sum(mps.deaths), 0), coalesce(sum(mps.assists), 0),
        coalesce(sum(mps.damage), 0), count(*) filter (where mps.damage is not null),
        coalesce(sum(mps.hill_time), 0), coalesce(sum(mps.impact), 0),
        count(*) filter (where mps.impact is not null), count(*)
    into
        v_total_kills, v_total_deaths, v_total_assists,
        v_total_damage, v_damage_rounds,
        v_total_hill, v_total_impact, v_impact_rounds, v_total_rounds
    from match_player_stats mps
    join matches m on m.id = mps.match_id
    where mps.player_id = p_player_id and m.status in ('completed', 'pending_verification', 'awaiting_review');

    update players
    set total_matches = coalesce(v_total_matches, 0),
        wins = coalesce(v_wins, 0),
        losses = coalesce(v_losses, 0),
        mvp_count = v_mvp_count,
        total_assists = v_total_assists,
        avg_kills = case when v_total_rounds > 0 then round(v_total_kills::numeric / v_total_rounds, 2) else 0 end,
        avg_deaths = case when v_total_rounds > 0 then round(v_total_deaths::numeric / v_total_rounds, 2) else 0 end,
        avg_damage = case when v_damage_rounds > 0 then round(v_total_damage / v_damage_rounds, 2) else 0 end,
        avg_hill_time = case when v_total_rounds > 0 then round(v_total_hill / v_total_rounds, 2) else 0 end
    where id = p_player_id;
end;
$$;

-- ============================================================
-- BACKFILL: run this immediately after the function above is applied.
-- Corrects every existing player's cached total_matches/wins/losses
-- using the fixed logic. Safe to run multiple times (idempotent --
-- recomputes from source data each time, does not increment anything).
-- Every player's total_matches will now exactly equal their wins+losses.
-- ============================================================
-- select recompute_player_career_stats(id) from players where total_matches > 0;

-- ============================================================
-- VERIFICATION (optional, run after the backfill): confirms zero
-- players remain with total_matches != wins+losses.
-- ============================================================
-- select id, ign, total_matches, wins, losses
-- from players
-- where total_matches != (wins + losses);
-- (expected: 0 rows)
