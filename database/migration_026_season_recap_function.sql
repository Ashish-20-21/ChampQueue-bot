-- migration_026_season_recap_function.sql
--
-- Season Recap: a single aggregate function backing the "showcase the
-- season" embed that fires before Hall of Fame. Deliberately separate
-- from the hof_* functions (migration_024) — this is season-wide
-- totals, not a per-category winner, and returns one row, not a
-- leaderboard.
--
-- Numbers verified by hand against real prod Season 1 data before this
-- was written (see database/season_recap_stats.sql /
-- diagnose_rounds_count.sql in this same directory for the full
-- verification trail):
--   - "matches" = distinct matches with real match_player_stats data,
--     NOT every row in `matches` with status='completed' — Season 1
--     prod has 391 total match rows but only 271 with actual recorded
--     gameplay (the rest are presumably test/bootstrap/incomplete rows
--     that reached 'completed' status without real scoreboard data).
--     Counting distinct matches_in_stats avoids publishing an inflated
--     number to players.
--   - "rounds" is the RO3-vs-RO1-agnostic total — an RO3 match
--     contributes up to 3, an RO1 match contributes 1, summed together
--     as one big season-wide number per the design decision to not
--     surface that historical distinction in a celebratory recap
--     (unlike Hall of Fame, where per-player accuracy across the
--     RO3->RO1 boundary mattered for who wins which category).
--   - kills/deaths/mvps use the same per-match-first aggregation as
--     migration_024's HOF functions, for the same reason (avoid
--     round-level triple-counting on pre-RO1 matches).
--   - hill_time is per-round, not per-player — deduped on
--     (match_id, round_number) before summing, since all 10 players in
--     a round share the same hill_time value.

create or replace function season_recap_stats(p_season_id bigint)
returns table (
    matches_played bigint,
    rounds_played bigint,
    unique_players bigint,
    total_kills bigint,
    total_deaths bigint,
    total_mvps_awarded bigint,
    total_hardpoint_hours numeric
)
language sql
stable
as $$
    with per_match_stats as (
        select mps.player_id, mps.match_id, sum(mps.kills) as match_kills, sum(mps.deaths) as match_deaths
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mps.player_id, mps.match_id
    ),
    per_match_mvp as (
        select mrr.player_id, mrr.match_id, bool_or(mrr.is_mvp) as was_mvp
        from match_round_results mrr
        join matches m on m.id = mrr.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mrr.player_id, mrr.match_id
    ),
    distinct_rounds as (
        select distinct mps.match_id, mps.round_number, mps.hill_time
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
    ),
    roster as (
        select mp.player_id, mp.match_id
        from match_players mp
        join matches m on m.id = mp.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
    )
    select
        (select count(distinct match_id) from per_match_stats) as matches_played,
        (select count(*) from distinct_rounds) as rounds_played,
        (select count(distinct player_id) from roster) as unique_players,
        coalesce((select sum(match_kills) from per_match_stats), 0) as total_kills,
        coalesce((select sum(match_deaths) from per_match_stats), 0) as total_deaths,
        coalesce((select count(*) from per_match_mvp where was_mvp), 0) as total_mvps_awarded,
        coalesce((select round(sum(hill_time) / 3600.0, 1) from distinct_rounds), 0) as total_hardpoint_hours;
$$;
