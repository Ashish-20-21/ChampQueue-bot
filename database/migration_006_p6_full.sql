-- P6: career stats, region leaderboard, weekly badges, new rank tiers.
-- Consolidated replacement for the original migration_006 +
-- migration_007 (2026-07-19) — folded into one file after finding
-- several issues across live testing: a UNION syntax error in
-- weekly_leaders, and a rank-display bug where the stored
-- players.current_rank column can silently disagree with what a
-- player's actual mmr maps to. Explained in full below so the reasoning
-- is on record, not just the fix.
--
-- SAFE TO RUN: run database/00_verify_before_revert.sql first — if
-- match_player_stats/match_round_results are both empty and no matches
-- are 'completed', this is a clean slate and the drop below loses
-- nothing. If that's not the case, stop and ask before running this.
--
-- Run after migration_005_correction_and_timeout.sql.

drop table if exists match_player_stats;

-- ---------------------------------------------------------------------
-- 1. match_player_stats — raw per-round stat rows.
-- ---------------------------------------------------------------------
-- These fields were already being extracted and validated in
-- cogs/match.py's _prepare_rounds (kills/deaths/assists/damage/hill_time/
-- impact/score all pass through _INTEGER_FIELDS / _HILL_TIME_RE checks)
-- but were discarded after validation instead of persisted. This table
-- is the missing write target — same row shape and same round-numbered
-- structure as match_round_results, just carrying the raw stats instead
-- of the MMR/position outcome.
--
-- Written at the same point in the flow as match_round_results (inside
-- match_submit, provisional/pre-approval), read only by the post-approval
-- aggregation step. Never itself written to by approve_ro3_match — MMR
-- and career-stat writes stay on separate tables so a correction to one
-- never has to reason about the other's constraints.
create table match_player_stats (
    id           bigserial primary key,
    match_id     bigint not null references matches(id) on delete cascade,
    round_number integer not null check (round_number in (1, 2, 3)),
    player_id    bigint not null references players(id),
    kills        integer not null,
    deaths       integer not null,
    assists      integer not null,
    damage       integer,                 -- nullable, matches _INTEGER_FIELDS' deliberate damage exclusion
    hill_time    numeric(6,2) not null,
    impact       numeric(6,2),
    score        integer not null,
    unique (match_id, round_number, player_id)
);

create index idx_match_player_stats_match on match_player_stats(match_id);
create index idx_match_player_stats_player on match_player_stats(player_id);

-- players.avg_kills / avg_deaths / avg_damage already exist (schema.sql).
-- avg_hill_time and total_assists did not — both are on the locked
-- /player-stats field list (2026-07-19) but had no column to land in.
alter table players add column if not exists avg_hill_time numeric(6,2) not null default 0;
alter table players add column if not exists total_assists integer not null default 0;

-- ---------------------------------------------------------------------
-- 2. New starting MMR — confirmed 150 (2026-07-19).
-- ---------------------------------------------------------------------
-- Old default (1000) was set back in P1/P2 under the ORIGINAL 100-point
-- tier bands, where 1000 was the very top (Titans). Under the new
-- 150-point bands, 1000 lands mid-scale (Grandmaster1) — a brand-new
-- player with zero matches would start there, which is wrong. 150 was
-- picked deliberately as a small head-start just above the Elite1 floor
-- (0), specifically to avoid a fresh player's very first loss pushing
-- them into the "no negative MMR" floor logic on day one.
--
-- This only changes the DEFAULT for future registrations — it does NOT
-- retroactively change any existing player's mmr. Existing players keep
-- whatever mmr they've already earned; only brand-new rows get 150
-- going forward.
alter table players alter column mmr set default 150;
alter table players alter column peak_mmr set default 150;

-- ---------------------------------------------------------------------
-- 3. Rank tiers — 150-point bands, confirmed 2026-07-19.
-- ---------------------------------------------------------------------
-- Elite1 0-150, Elite2 151-300, PRO1 301-450, PRO2 451-600, Master1
-- 601-750, Master2 751-900, GM1 901-1050, GM2 1051-1200, Legendary1
-- 1201-1350, Legendary2 1351-1500, Titans 1501+.
--
-- IMPORTANT — read this before touching rank logic anywhere:
-- There is NO single source of truth for "what rank does this MMR map
-- to." The same CASE chain is duplicated in THREE places, kept in sync
-- by hand:
--   1. approve_ro3_match (below) — the only place that WRITES
--      players.current_rank / peak_rank, and only at match-approval time.
--   2. mmr_engine.derive_rank() (Python) — used by /whoami, /rank-progress,
--      and player_stats_card to DISPLAY a rank without touching the DB.
--   3. region_leaderboard() (below) — used by the leaderboard panel,
--      also DISPLAY-only, computed inline in SQL rather than calling (1).
--
-- WHY (1) is not enough on its own, and why (2)/(3) exist: players.mmr
-- can change without ever going through approve_ro3_match — a manual
-- admin correction via direct Supabase edit, or /admin-adjust-mmr, both
-- write players.mmr directly. If display always trusted the STORED
-- current_rank column, it would show a stale rank until that player's
-- NEXT real match approval — confirmed live 2026-07-19, test-seeded
-- players showed "Titans" and "Elite" at MMR values that mapped to
-- Grandmaster1/Grandmaster2 under the real bands, because their stored
-- current_rank predated this migration and nothing had re-derived it.
-- (2) and (3) fix this by computing rank fresh from mmr on every read,
-- so the displayed rank is always correct regardless of how mmr got to
-- its current value — no refresh, no manual title edit, no extra step.
-- The STORED column can still lag until a real approval happens; that's
-- harmless now since nothing important reads it directly anymore (see
-- database/backfill_current_rank.sql for an optional one-time cosmetic
-- fix to the stored column itself, not required for correctness).
--
-- If the tier bands ever change again: update all three places in the
-- same commit, or displayed ranks and stored ranks will disagree again.
create or replace function approve_ro3_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language plpgsql
as $$
declare
    v_match matches%rowtype;
begin
    select * into v_match from matches where id = p_match_id for update;
    if not found then
        raise exception 'match % does not exist', p_match_id;
    end if;
    if v_match.status <> 'pending_verification' then
        raise exception 'match % is not pending verification', p_match_id;
    end if;
    if (select count(*) from match_round_results where match_id = p_match_id) <> 30 then
        raise exception 'match % must have exactly 30 round-result rows', p_match_id;
    end if;

    return query
    with deltas as (
        select mrr.player_id, sum(mrr.mmr_delta)::integer as total_delta
        from match_round_results mrr
        where mrr.match_id = p_match_id
        group by mrr.player_id
    ), updated_players as (
        update players p
        set mmr = greatest(0, p.mmr + d.total_delta),
            peak_mmr = greatest(p.peak_mmr, greatest(0, p.mmr + d.total_delta)),
            current_rank = case
                when greatest(0, p.mmr + d.total_delta) >= 1501 then 'Titans'
                when greatest(0, p.mmr + d.total_delta) >= 1351 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Legendary1'
                when greatest(0, p.mmr + d.total_delta) >= 1051 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 901  then 'Grandmaster1'
                when greatest(0, p.mmr + d.total_delta) >= 751  then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 601  then 'Master1'
                when greatest(0, p.mmr + d.total_delta) >= 451  then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 301  then 'PRO1'
                when greatest(0, p.mmr + d.total_delta) >= 151  then 'Elite2'
                else 'Elite1'
            end,
            current_division = '',
            peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
                when greatest(0, p.mmr + d.total_delta) >= 1501 then 'Titans' when greatest(0, p.mmr + d.total_delta) >= 1351 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Legendary1' when greatest(0, p.mmr + d.total_delta) >= 1051 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 901  then 'Grandmaster1' when greatest(0, p.mmr + d.total_delta) >= 751 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 601  then 'Master1' when greatest(0, p.mmr + d.total_delta) >= 451 then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 301  then 'PRO1' when greatest(0, p.mmr + d.total_delta) >= 151 then 'Elite2' else 'Elite1' end
                else p.peak_rank end,
            updated_at = now()
        from deltas d
        where p.id = d.player_id
        returning p.id, p.mmr - d.total_delta as before_mmr, p.mmr as after_mmr, d.total_delta
    ), updated_match_players as (
        update match_players mp
        set mmr_before = up.before_mmr, mmr_after = up.after_mmr, mmr_change = up.total_delta
        from updated_players up
        where mp.match_id = p_match_id and mp.player_id = up.id
        returning up.id, up.before_mmr, up.after_mmr, up.total_delta
    )
    select * from updated_match_players;

    update matches
    set status = 'completed', completed_at = now(), approved_by = p_approved_by, approved_at = now()
    where id = p_match_id;
end;
$$;

-- ---------------------------------------------------------------------
-- 4. Career-stat recompute function — idempotent, one player at a time.
-- ---------------------------------------------------------------------
-- Called from cogs/match.py._do_approve, right after approve_ro3_match
-- succeeds, once per player in that match (10 calls, bounded — not a
-- full-leaderboard scan). Fully recomputes from match_player_stats +
-- match_round_results across every completed match the player has ever
-- been in, rather than incrementing a running total — so a later admin
-- correction to a match_player_stats row self-corrects the next time
-- this runs, with no separate reversal logic needed anywhere.
--
-- win/loss is counted at the ROUND level, not the match level — RO3 has
-- no single "match winner" (all 3 rounds always play, independently),
-- so wins/losses is simply every round's outcome, summed across career.
-- A round's outcome is already determined by the time it reaches
-- match_round_results: mmr_delta > 0 means that round's position-table
-- delta came from the winning side. Every completed match contributes
-- exactly 3 to wins+losses combined.
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
    where mrr.player_id = p_player_id and m.status = 'completed';

    select count(distinct mrr.match_id) into v_total_matches
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status = 'completed';

    select count(*) into v_mvp_count
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status = 'completed' and mrr.is_mvp = true;

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
    where mps.player_id = p_player_id and m.status = 'completed';

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

-- ---------------------------------------------------------------------
-- 5. Region-scoped leaderboard, full roster (no LIMIT — grows
--    automatically as registration adds more players).
-- ---------------------------------------------------------------------
create or replace function region_leaderboard(p_region text)
returns table (
    id bigint, ign text, mmr integer, peak_mmr integer,
    current_rank text, wins integer, losses integer, mvp_count integer
)
language sql
stable
as $$
    -- current_rank is DERIVED from mmr here, not read from
    -- players.current_rank — see the big comment above approve_ro3_match
    -- for the full "why" (players.current_rank is write-only-at-approval,
    -- can go stale relative to the actual mmr, confirmed live 2026-07-19).
    select id, ign, mmr, peak_mmr,
        case
            when greatest(0, mmr) >= 1501 then 'Titans'
            when greatest(0, mmr) >= 1351 then 'Legendary2'
            when greatest(0, mmr) >= 1201 then 'Legendary1'
            when greatest(0, mmr) >= 1051 then 'Grandmaster2'
            when greatest(0, mmr) >= 901  then 'Grandmaster1'
            when greatest(0, mmr) >= 751  then 'Master2'
            when greatest(0, mmr) >= 601  then 'Master1'
            when greatest(0, mmr) >= 451  then 'PRO2'
            when greatest(0, mmr) >= 301  then 'PRO1'
            when greatest(0, mmr) >= 151  then 'Elite2'
            else 'Elite1'
        end as current_rank,
        wins, losses, mvp_count
    from players
    where region = p_region and status = 'approved'
    order by mmr desc, id asc;
$$;

-- ---------------------------------------------------------------------
-- 6. Weekly achievement leaders, region-scoped.
-- ---------------------------------------------------------------------
-- One call returns all 5 categories at once. Computed live at read-time,
-- not stored anywhere — a player_achievements-style earn/store model
-- doesn't fit a rolling "true this week" computation (that table is
-- shaped for permanent one-time earns), so this avoids a write/cleanup
-- path for something that can just be queried fresh each time cheaply.
--
-- Ties: lowest player_id wins — arbitrary but deterministic, avoids a
-- nondeterministic winner on a genuine tie. Revisit if this ever matters
-- at your real player counts.
--
-- Each category's "top row" is its own named CTE with its own
-- ORDER BY + LIMIT 1, then the CTEs are unioned together at the end.
-- NOT five bare "select ... limit 1" branches joined directly by
-- UNION ALL — that IS a syntax error in Postgres (hit this live,
-- 2026-07-19: "syntax error at or near union"), because a LIMIT sitting
-- directly inside a union branch is ambiguous — the parser can't tell if
-- it binds to that branch or the whole union. Wrapping each ranked pick
-- in its own CTE first removes the ambiguity.
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
    ),
    top_mvp as (
        select 'most_mvp' as category, player_id, mvp_count::numeric as value
        from per_player order by mvp_count desc, player_id asc limit 1
    ), top_kills as (
        select 'top_kills' as category, player_id, total_kills::numeric as value
        from per_player order by total_kills desc, player_id asc limit 1
    ), top_obj as (
        select 'top_obj' as category, player_id, total_hill::numeric as value
        from per_player order by total_hill desc, player_id asc limit 1
    ), top_impact as (
        select 'top_impact' as category, player_id, total_impact::numeric as value
        from per_player where total_impact is not null order by total_impact desc, player_id asc limit 1
    ), top_matches as (
        select 'most_matches' as category, player_id, matches_played::numeric as value
        from per_player order by matches_played desc, player_id asc limit 1
    )
    select category, player_id, value from top_mvp
    union all
    select category, player_id, value from top_kills
    union all
    select category, player_id, value from top_obj
    union all
    select category, player_id, value from top_impact
    union all
    select category, player_id, value from top_matches;
$$;
