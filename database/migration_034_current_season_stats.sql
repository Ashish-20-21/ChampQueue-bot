-- migration_034_current_season_stats.sql
-- Adds current_season_stats(player_id, season_id) — season-scoped
-- equivalent of the players table's all-time stat columns. Purely
-- additive: creates one new function, touches no existing table,
-- column, or function. Read-only (SELECT-only body), no writes.
--
-- Same pattern as hof_best_kd / hof_most_mvps / region_leaderboard —
-- computed fresh on every call, nothing stored, nothing precomputed.
-- Win/loss uses the same mmr_delta-minus-MVP-bonus signal as
-- recompute_player_career_stats() (see engineering-rules' MVP-sign-flip
-- trap: a losing team's position-1 MVP scores -3+5=+2, positive despite
-- losing — never infer win/loss from mmr_delta sign alone).
--
-- mmr_delta and is_mvp are read from match_round_results, NOT
-- match_players -- confirmed live via:
--   SELECT column_name, table_name FROM information_schema.columns
--   WHERE column_name IN ('mmr_delta','mmr_change','is_mvp');
-- mmr_delta only exists on match_round_results (match_players only has
-- mmr_change, the final summed value, not the per-round delta). Both
-- mmr_delta and is_mvp are pulled from the SAME row on that same table
-- so the win/loss derivation never mixes values across two tables.
--
-- Verified safe against RO3 leftover data: match_round_results has
-- multiple rows per player per match for old RO3 matches (Season 1),
-- but confirmed empty for any Season 2 (RO1) match -- the season_id
-- filter naturally excludes the old multi-row data, so no double-
-- counting risk for current-season win/loss/MVP counts.

CREATE OR REPLACE FUNCTION current_season_stats(p_player_id bigint, p_season_id bigint)
RETURNS TABLE (
    matches bigint,
    total_kills bigint,
    total_deaths bigint,
    avg_kills numeric,
    avg_hill_time numeric,
    wins bigint,
    losses bigint,
    mvps bigint
) AS $$
BEGIN
    RETURN QUERY
    WITH stats AS (
        SELECT mps.match_id, mps.kills, mps.deaths, mps.hill_time
        FROM match_player_stats mps
        JOIN matches m ON mps.match_id = m.id
        WHERE mps.player_id = p_player_id
          AND m.season_id = p_season_id
          AND m.status = 'completed'
    ),
    results AS (
        SELECT mrr.mmr_delta, mrr.is_mvp
        FROM match_round_results mrr
        JOIN matches m ON mrr.match_id = m.id
        WHERE mrr.player_id = p_player_id
          AND m.season_id = p_season_id
          AND m.status = 'completed'
    )
    SELECT
        (SELECT COUNT(DISTINCT match_id) FROM stats),
        COALESCE((SELECT SUM(kills) FROM stats), 0),
        COALESCE((SELECT SUM(deaths) FROM stats), 0),
        CASE WHEN (SELECT COUNT(DISTINCT match_id) FROM stats) > 0
            THEN ROUND((SELECT SUM(kills) FROM stats)::numeric / (SELECT COUNT(DISTINCT match_id) FROM stats), 2)
            ELSE 0 END,
        CASE WHEN (SELECT COUNT(DISTINCT match_id) FROM stats) > 0
            THEN ROUND((SELECT SUM(hill_time) FROM stats) / (SELECT COUNT(DISTINCT match_id) FROM stats), 2)
            ELSE 0 END,
        COALESCE((SELECT COUNT(*) FROM results WHERE (mmr_delta - CASE WHEN is_mvp THEN 5 ELSE 0 END) > 0), 0),
        COALESCE((SELECT COUNT(*) FROM results WHERE (mmr_delta - CASE WHEN is_mvp THEN 5 ELSE 0 END) <= 0), 0),
        COALESCE((SELECT COUNT(*) FROM results WHERE is_mvp), 0);
END;
$$ LANGUAGE plpgsql STABLE;