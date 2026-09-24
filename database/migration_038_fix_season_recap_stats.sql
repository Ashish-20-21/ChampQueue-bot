-- ============================================================
-- MIGRATION 037: Fix season_recap_stats() — rounds_played and
--                total_mvps_awarded both wrong
-- ------------------------------------------------------------
-- Bug 1: rounds_played counted raw match_player_stats rows
--        (10 per round) instead of distinct (match_id, round_number)
--        pairs. For S2 RO1: returned 5,521 instead of 580.
--
-- Bug 2: total_mvps_awarded used COUNT(DISTINCT ...) with a CASE
--        expression that collapsed per-match MVP pairs, returning
--        189 instead of the correct 1,160 (2 MVPs per match,
--        1 per team, sourced from match_round_results.is_mvp).
--
-- Fix: rewrite the function with correct aggregation logic.
--      Joins match_round_results for MVP count.
--      Pure read function — no schema changes, no data changes.
-- ============================================================

CREATE OR REPLACE FUNCTION season_recap_stats(p_season_id integer)
RETURNS TABLE (
    matches_played        bigint,
    rounds_played         bigint,
    unique_players        bigint,
    total_kills           bigint,
    total_deaths          bigint,
    total_mvps_awarded    bigint,
    total_hardpoint_hours numeric
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT
        COUNT(DISTINCT mps.match_id)                            AS matches_played,
        COUNT(DISTINCT (mps.match_id, mps.round_number))        AS rounds_played,
        COUNT(DISTINCT mps.player_id)                           AS unique_players,
        SUM(mps.kills)                                          AS total_kills,
        SUM(mps.deaths)                                         AS total_deaths,
        (SELECT COUNT(*)
         FROM match_round_results mrr2
         JOIN matches m2 ON m2.id = mrr2.match_id
         WHERE m2.season_id = p_season_id
           AND mrr2.is_mvp = true)::bigint                      AS total_mvps_awarded,
        ROUND(SUM(mps.hill_time) / 3600.0, 1)                  AS total_hardpoint_hours
    FROM match_player_stats mps
    JOIN matches m ON m.id = mps.match_id
    WHERE m.season_id = p_season_id;
END;
$$;
