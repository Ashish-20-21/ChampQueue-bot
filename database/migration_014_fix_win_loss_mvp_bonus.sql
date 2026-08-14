-- migration_014_fix_win_loss_mvp_bonus.sql
-- Fix 2026-08-14: recompute_player_career_stats() has been silently
-- miscounting wins/losses since it was introduced (migration_006/011).
--
-- Bug: wins/losses were derived from the SIGN of mmr_delta alone
-- (mmr_delta > 0 => "win", mmr_delta <= 0 => "loss"). This is wrong
-- whenever is_mvp is true: the +5 MVP bonus can push a LOSING round's
-- delta positive. Confirmed via services/mmr_engine.py's real delta
-- table (_WINNING_DELTAS / _LOSING_DELTAS): a position-1 loss is -3,
-- but with the MVP bonus becomes -3 + 5 = +2 — a positive number
-- produced entirely by a loss. Every losing-side MVP performance was
-- being counted as a win.
--
-- Confirmed impact on a real player (SumitCantSnipe, player_id 184,
-- 2026-08-14 investigation): stored wins/losses showed 30W-8L. Manually
-- decoding every round via (mmr_delta, is_mvp) against the real delta
-- table gives the true result: 16W-22L at the round level — more than
-- half of his true losses (12 of 22) were MVP-on-losing-side rounds
-- miscounted as wins. This bug affects every player who has ever
-- earned MVP while losing a round, i.e. a large fraction of the
-- playerbase, not an isolated case.
--
-- Fix: strip the MVP bonus BEFORE checking the sign. Base win deltas
-- (9/8/6/4/3 by position) are always positive; base loss deltas
-- (-3/-4/-6/-8/-9 by position) are always negative — with zero
-- ambiguity, regardless of MVP status. No position column lookup
-- needed; (mmr_delta - mvp_bonus) alone is a fully reliable signal.
--
-- This migration ONLY changes the wins/losses calculation inside the
-- function. total_matches, mvp_count, avg_kills/deaths/damage/hill_time
-- were already computed correctly and are unchanged.
--
-- IMPORTANT: replacing this function does NOT retroactively fix any
-- player's currently-stored wins/losses columns — those are cached on
-- the players row and only refresh when this function actually runs
-- (on match submission, approval, or /admin manual trigger). Run the
-- one-line backfill at the bottom of this file immediately after
-- applying this migration to correct every existing player's numbers
-- in one pass.

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
    -- FIXED: strip the +5 MVP bonus before checking sign, so a losing
    -- round with MVP (e.g. -3 + 5 = +2) is correctly counted as a loss.
    select count(*) filter (where (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0),
           count(*) filter (where (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0)
    into v_wins, v_losses
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status in ('completed', 'pending_verification', 'awaiting_review');

    select count(distinct mrr.match_id) into v_total_matches
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
    set total_matches = v_total_matches,
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
-- Corrects every existing player's cached wins/losses using the fixed
-- logic. Safe to run multiple times (idempotent — recomputes from
-- source data each time, does not increment anything).
-- ============================================================
-- select recompute_player_career_stats(id) from players where total_matches > 0;
