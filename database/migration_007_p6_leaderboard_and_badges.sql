-- P6 part 2: region-scoped leaderboard read + weekly achievement badges.
-- Both computed live at read-time, not stored/written anywhere — a
-- player_achievements-style earn/store model doesn't fit a rolling
-- "true this week" computation (it's designed for permanent one-time
-- earns), so this avoids adding a write/cleanup path for something that
-- can just be queried fresh each time cheaply.

-- ---------------------------------------------------------------------
-- 1. Region-scoped leaderboard, full roster (no LIMIT baked in — the bot
--    paginates client-side once past Discord's embed size, but the query
--    itself always returns everyone in the region so "add players later"
--    keeps showing up automatically).
-- ---------------------------------------------------------------------
create or replace function region_leaderboard(p_region text)
returns table (
    id bigint, ign text, mmr integer, peak_mmr integer,
    current_rank text, wins integer, losses integer, mvp_count integer
)
language sql
stable
as $$
    select id, ign, mmr, peak_mmr, current_rank, wins, losses, mvp_count
    from players
    where region = p_region and status = 'approved'
    order by mmr desc, id asc;
$$;

-- ---------------------------------------------------------------------
-- 2. Weekly achievement leaders, region-scoped. One call returns all 5
--    categories at once (each just the single top player_id + value) so
--    /player-stats doesn't need 5 round-trips to figure out which
--    badges apply to one card.
-- ---------------------------------------------------------------------
-- Ties: first-registered player_id wins (min(player_id)) — arbitrary but
-- deterministic, avoids the query returning a nondeterministic winner on
-- a genuine tie. Not spec'd explicitly; revisit if this ever matters at
-- your actual player counts.
create or replace function weekly_leaders(p_region text)
returns table (category text, player_id bigint, value numeric)
language sql
stable
as $$
    with week_rounds as (
        select mps.*, mrr.is_mvp, mrr.team
        from match_player_stats mps
        join match_round_results mrr
            on mrr.match_id = mps.match_id and mrr.round_number = mps.round_number and mrr.player_id = mps.player_id
        join matches m on m.id = mps.match_id
        join players p on p.id = mps.player_id
        where m.status = 'completed'
          and m.completed_at >= now() - interval '7 days'
          and p.region = p_region
    ),
    per_player as (
        select player_id,
               sum(kills) as total_kills,
               sum(hill_time) as total_hill,
               sum(impact) filter (where impact is not null) as total_impact,
               count(*) filter (where is_mvp) as mvp_count,
               count(distinct match_id) as matches_played
        from week_rounds
        group by player_id
    )
    select 'most_mvp', player_id, mvp_count::numeric from per_player order by mvp_count desc, player_id asc limit 1
    union all
    select 'top_kills', player_id, total_kills::numeric from per_player order by total_kills desc, player_id asc limit 1
    union all
    select 'top_obj', player_id, total_hill::numeric from per_player order by total_hill desc, player_id asc limit 1
    union all
    select 'top_impact', player_id, total_impact::numeric from per_player where total_impact is not null order by total_impact desc, player_id asc limit 1
    union all
    select 'most_matches', player_id, matches_played::numeric from per_player order by matches_played desc, player_id asc limit 1;
$$;
