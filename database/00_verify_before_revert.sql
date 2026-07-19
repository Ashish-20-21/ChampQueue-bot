-- Run this FIRST, before anything else. Confirms it's safe to drop and
-- recreate match_player_stats — expect all three counts to be 0.
select
    (select count(*) from match_player_stats) as match_player_stats_rows,
    (select count(*) from match_round_results) as match_round_results_rows,
    (select count(*) from matches where status = 'completed') as completed_matches;
