-- migration_011_provisional_stats.sql
-- Reform 2026-07-29: split "career stats visible" from "MMR committed".
--
-- Problem: recompute_player_career_stats() only ever counted matches with
-- status = 'completed' — the same gate that guards MMR. In practice, OCR
-- can pass cleanly on some/all rounds while the match still ends up
-- needing review for unrelated reasons on one round: unreadable map
-- string, a live player-transfer edge case, a team-side bug, etc.
-- Confirmed live (2026-07-29, match CQ-9193): a match with 1 of 3 rounds
-- clean got ALL round data discarded — not just the bad round — because
-- match_submit's write step ran only after review_reasons was confirmed
-- empty. Fixed on the write side (cogs/match.py: per-round "clean" flag,
-- clean rounds now write to match_player_stats/match_round_results
-- unconditionally, before the match is routed to awaiting_review).
--
-- This migration is the matching read-side fix: widen the filter so
-- recompute_player_career_stats also counts 'pending_verification' (OCR
-- fully passed, waiting on approval) AND 'awaiting_review' (OCR partially
-- passed, at least one round flagged) matches, not just 'completed' ones.
-- This is a pure read-path change — it does not touch matches.status
-- itself, does not touch players.mmr/current_rank, and does not change
-- what approve_ro3_match is allowed to do. MMR stays exactly as strict as
-- before, gated only on real approval.
--
-- Known, accepted tradeoff: a match that reaches pending_verification or
-- awaiting_review but is later cancelled/abandoned without ever being
-- approved or corrected will have already contributed to a player's
-- visible stats, with no automatic "un-recompute" on cancellation.
-- Checked against the actual cancel path (admin-scrap-match) — that only
-- fires before screenshot submission, i.e. before a match can ever reach
-- either of these statuses, so this gap is not reachable in practice.
-- Accepted as negligible.

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
    select count(*) filter (where mrr.mmr_delta > 0),
           count(*) filter (where mrr.mmr_delta <= 0)
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
        avg_hill_time = case when v_total_rounds > 0 then round(v_total_hill / v_total_rounds, 2) else 0 end,
        updated_at = now()
    where id = p_player_id;
end;
$$;
