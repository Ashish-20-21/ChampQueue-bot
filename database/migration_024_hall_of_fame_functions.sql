-- migration_024_hall_of_fame_functions.sql
--
-- Season 1 end-of-season prep, part 2: Hall of Fame category functions.
--
-- Following the established house pattern (region_leaderboard in
-- migration_006_p6_full.sql): aggregation logic lives in a `language sql
-- stable` Postgres function, called from Python via .rpc(), never as
-- raw SQL from the app layer or via the table builder for anything this
-- complex. Each function takes p_season_id so it works for any season,
-- not hardcoded to season_id=1 — Season 2 gets these for free.
--
-- Every category with a minimum-sample floor uses >= 8 matches, decided
-- against real Season 1 data (see hof_consistency_scout.sql /
-- hof_remaining_categories.sql in this same directory — that's where
-- the 8-match number and the "obj time" category's removal came from,
-- not picked arbitrarily here).
--
-- Two categories intentionally have NO floor:
--   - Highest Kills (total) — season-cumulative, low-volume players
--     simply won't lead it, no floor needed to keep it fair.
--   - Highest Rank/MMR — current mmr isn't season-cumulative the way
--     the others are; explicit decision, no floor.
--
-- CORRECTION found via test-DB verification (not test-only — this
-- applies equally to prod, see below): match_player_stats and
-- match_round_results both still allow round_number in (1,2,3) —
-- migration_015_ro1.sql deliberately left this unconstrained "for
-- parity with historical RO3 rows" rather than narrowing it to =1.
-- Any match played before the RO1 cutover has up to 3 rows per player
-- in these two tables, not 1. match_players has NO round_number column
-- at all (confirmed against schema.sql) and is genuinely one row per
-- match — hof_most_consistent and hof_fastest_climber, which read
-- match_players, were never affected by this.
--
-- The other four categories (highest_total_kills, best_avg_kills,
-- best_avg_deaths, best_kd, most_mvps) originally read raw rows from
-- match_player_stats/match_round_results directly, which for a pre-RO1
-- match silently double/triple-counted kills, deaths, match counts,
-- and MVP awards. Confirmed live in test DB: a player showed 50 raw
-- stats rows but only 29 distinct matches and 28 match_players roster
-- rows for the same season — the extra rows were real round_number
-- 1/2/3 data from real pre-RO1 test matches, not corruption. Since
-- Season 1 in prod spans the actual RO3→RO1 cutover (Aug 14), it will
-- have the identical shape on real matches, so this fix is not
-- optional/test-only. Fixed by aggregating to one row per
-- (player, match) in a CTE first — summing kills/deaths, OR-ing
-- is_mvp — before counting/summing across matches. A true RO1 match
-- (single round_number=1 row) collapses to itself with zero behavior
-- change; a pre-RO1 3-round match now correctly contributes once.
--
-- kills/deaths live in match_player_stats; is_mvp lives in
-- match_round_results — confirmed as separate tables via migration_017's
-- insert columns, kept as separate functions rather than one join to
-- avoid a fan-out bug.

-- ============================================================
-- 1. Most Consistent (win rate, min 8 matches)
-- ============================================================
create or replace function hof_most_consistent(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, wins integer, win_rate_pct numeric
)
language sql
stable
as $$
    select
        p.id, p.ign, p.discord_id,
        count(mp.player_id)::integer as matches_played,
        sum(case when (mp.mmr_change - case when mp.is_mvp then 5 else 0 end) > 0 then 1 else 0 end)::integer as wins,
        round(100.0 * sum(case when (mp.mmr_change - case when mp.is_mvp then 5 else 0 end) > 0 then 1 else 0 end)
              / nullif(count(mp.player_id), 0), 1) as win_rate_pct
    from match_players mp
    join matches m on m.id = mp.match_id
    join players p on p.id = mp.player_id
    where m.season_id = p_season_id
      and m.status = 'completed'
    group by p.id, p.ign, p.discord_id
    having count(mp.player_id) >= 8
    order by win_rate_pct desc, matches_played desc
    limit 1;
$$;

-- ============================================================
-- 2. Fastest Climber (MMR gained per match, min 8 matches)
-- ============================================================
create or replace function hof_fastest_climber(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, mmr_gained integer, mmr_per_match numeric
)
language sql
stable
as $$
    select
        p.id, p.ign, p.discord_id,
        count(mp.player_id)::integer as matches_played,
        sum(mp.mmr_change)::integer as mmr_gained,
        round(sum(mp.mmr_change)::numeric / nullif(count(mp.player_id), 0), 2) as mmr_per_match
    from match_players mp
    join matches m on m.id = mp.match_id
    join players p on p.id = mp.player_id
    where m.season_id = p_season_id
      and m.status = 'completed'
    group by p.id, p.ign, p.discord_id
    having count(mp.player_id) >= 8
    order by mmr_per_match desc
    limit 1;
$$;

-- ============================================================
-- 3. Highest Kills (total, season) — no floor, see header note.
-- ============================================================
-- FIX (found via test-DB verification, applies equally to prod): the
-- first version of this function did `count(mps.player_id)` and
-- `sum(mps.kills)` directly against match_player_stats — that table is
-- per-ROUND, not per-match. migration_015_ro1.sql deliberately left
-- round_number allowing 1-3 "for parity with historical RO3 rows" —
-- any match played before the RO1 cutover has up to 3 rows per player
-- in this table, not 1. Reading raw rows as if one row = one match
-- silently triples that player's kills/match-count/deaths for every
-- pre-RO1 match they played. Confirmed live: a test player showed
-- "50 matches" via this query's old raw-row count but only 29 distinct
-- match_ids and 28 match_players roster rows for the same season.
-- Fix: aggregate to one row per (player, match) FIRST via a CTE, sum
-- kills/deaths within a match, THEN count/sum across matches. A true
-- RO1 match (single round_number=1 row) collapses to itself with no
-- behavior change; a pre-RO1 3-round match now correctly contributes
-- once, with its 3 rounds' kills summed into that one match's total.
create or replace function hof_highest_total_kills(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, total_kills bigint
)
language sql
stable
as $$
    with per_match as (
        select mps.player_id, mps.match_id, sum(mps.kills) as match_kills
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mps.player_id, mps.match_id
    )
    select
        p.id, p.ign, p.discord_id,
        count(pm.match_id)::integer as matches_played,
        sum(pm.match_kills) as total_kills
    from per_match pm
    join players p on p.id = pm.player_id
    group by p.id, p.ign, p.discord_id
    order by total_kills desc
    limit 1;
$$;

-- ============================================================
-- 4. Best Avg Kills (min 8 matches)
-- ============================================================
-- Same per-match aggregation fix as hof_highest_total_kills above —
-- avg(mps.kills) directly would average across ROUNDS, not matches,
-- so a pre-RO1 match's 3 rounds would count as 3 separate samples in
-- the average instead of being one match's per-match kill total.
create or replace function hof_best_avg_kills(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, avg_kills numeric
)
language sql
stable
as $$
    with per_match as (
        select mps.player_id, mps.match_id, sum(mps.kills) as match_kills
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mps.player_id, mps.match_id
    )
    select
        p.id, p.ign, p.discord_id,
        count(pm.match_id)::integer as matches_played,
        round(avg(pm.match_kills), 2) as avg_kills
    from per_match pm
    join players p on p.id = pm.player_id
    group by p.id, p.ign, p.discord_id
    having count(pm.match_id) >= 8
    order by avg_kills desc
    limit 1;
$$;

-- ============================================================
-- 5. Best Avg Deaths (fewest, min 8 matches)
-- ============================================================
-- Same fix — per-match sum of deaths, not per-round average.
create or replace function hof_best_avg_deaths(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, avg_deaths numeric
)
language sql
stable
as $$
    with per_match as (
        select mps.player_id, mps.match_id, sum(mps.deaths) as match_deaths
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mps.player_id, mps.match_id
    )
    select
        p.id, p.ign, p.discord_id,
        count(pm.match_id)::integer as matches_played,
        round(avg(pm.match_deaths), 2) as avg_deaths
    from per_match pm
    join players p on p.id = pm.player_id
    group by p.id, p.ign, p.discord_id
    having count(pm.match_id) >= 8
    order by avg_deaths asc
    limit 1;
$$;


-- ============================================================
-- 6. Most MVPs (season, min 8 matches) — from match_round_results.
-- ============================================================
-- Same round-vs-match bug as categories 3/4/5/8: match_round_results
-- also has round_number check(in 1,2,3) — a pre-RO1 match can have up
-- to 3 rows per player here too. Raw count(mrr.player_id) as
-- "matches_played" was actually counting rounds, and count(*) filter
-- (where is_mvp) could double/triple-count a player who was MVP in
-- more than one round of the SAME pre-RO1 match — one match, counted
-- as multiple MVPs. Fix: collapse to one row per (player, match) first
-- — was_mvp_this_match true if MVP in ANY round of that match — then
-- count/sum across matches.
create or replace function hof_most_mvps(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, mvp_count bigint
)
language sql
stable
as $$
    with per_match as (
        select mrr.player_id, mrr.match_id,
               bool_or(mrr.is_mvp) as was_mvp_this_match
        from match_round_results mrr
        join matches m on m.id = mrr.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mrr.player_id, mrr.match_id
    )
    select
        p.id, p.ign, p.discord_id,
        count(pm.match_id)::integer as matches_played,
        count(*) filter (where pm.was_mvp_this_match) as mvp_count
    from per_match pm
    join players p on p.id = pm.player_id
    group by p.id, p.ign, p.discord_id
    having count(pm.match_id) >= 8
    order by mvp_count desc, matches_played desc
    limit 1;
$$;

-- ============================================================
-- 7. Most Matches Played (season) — no floor by definition.
-- ============================================================
create or replace function hof_most_matches_played(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played bigint
)
language sql
stable
as $$
    select
        p.id, p.ign, p.discord_id,
        count(mp.player_id) as matches_played
    from match_players mp
    join matches m on m.id = mp.match_id
    join players p on p.id = mp.player_id
    where m.season_id = p_season_id
      and m.status = 'completed'
    group by p.id, p.ign, p.discord_id
    order by matches_played desc
    limit 1;
$$;

-- ============================================================
-- 8. Best K/D (season, min 8 matches) — sum(kills)/sum(deaths), NOT
-- avg-of-per-match-ratios (per DEV_NOTES: same principle as the
-- mmr_delta win/loss fix — sum-then-divide, never average a ratio).
-- Same per-match aggregation fix as categories 3-5 above: sum kills
-- and deaths per match first, then across matches, so pre-RO1 3-round
-- matches don't triple-count into the ratio.
-- ============================================================
create or replace function hof_best_kd(p_season_id bigint)
returns table (
    player_id bigint, ign text, discord_id text,
    matches_played integer, total_kills bigint, total_deaths bigint, kd_ratio numeric
)
language sql
stable
as $$
    with per_match as (
        select mps.player_id, mps.match_id,
               sum(mps.kills) as match_kills, sum(mps.deaths) as match_deaths
        from match_player_stats mps
        join matches m on m.id = mps.match_id
        where m.season_id = p_season_id
          and m.status = 'completed'
        group by mps.player_id, mps.match_id
    )
    select
        p.id, p.ign, p.discord_id,
        count(pm.match_id)::integer as matches_played,
        sum(pm.match_kills) as total_kills,
        sum(pm.match_deaths) as total_deaths,
        round(sum(pm.match_kills)::numeric / nullif(sum(pm.match_deaths), 0), 2) as kd_ratio
    from per_match pm
    join players p on p.id = pm.player_id
    group by p.id, p.ign, p.discord_id
    having count(pm.match_id) >= 8
    order by kd_ratio desc nulls last
    limit 1;
$$;

-- ============================================================
-- 9. Highest Rank/MMR (current) — no floor, no season filter at all:
-- this reads live players.mmr, same source region_leaderboard already
-- uses. Deliberately NOT season-scoped to match_players, since it's a
-- snapshot of where a player stands right now, not a season total.
-- ============================================================
create or replace function hof_highest_mmr()
returns table (
    player_id bigint, ign text, discord_id text, mmr integer, current_rank text
)
language sql
stable
as $$
    select id, ign, discord_id, mmr, current_rank
    from players
    where status = 'approved'
    order by mmr desc
    limit 1;
$$;
