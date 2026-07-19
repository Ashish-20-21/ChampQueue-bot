-- P6: per-round raw stat storage + career aggregation support, plus the
-- new 150-point rank bands. Additive only — does not touch matches,
-- match_round_results, or match_players' existing columns/constraints.
--
-- Run after migration_004_ro3_mmr.sql.

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
create table if not exists match_player_stats (
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

create index if not exists idx_match_player_stats_match on match_player_stats(match_id);
create index if not exists idx_match_player_stats_player on match_player_stats(player_id);

-- players.avg_kills / avg_deaths / avg_damage already exist (schema.sql).
-- avg_hill_time and total_assists do not — both are on the locked
-- /player-stats field list (2026-07-19) but have no column to land in
-- yet. Adding them here since recompute_player_career_stats (below)
-- needs somewhere to write them.
alter table players add column if not exists avg_hill_time numeric(6,2) not null default 0;
alter table players add column if not exists total_assists integer not null default 0;

-- ---------------------------------------------------------------------
-- 2. New rank tiers — 150-point bands, confirmed 2026-07-19.
-- ---------------------------------------------------------------------
-- Elite1 0-150, Elite2 151-300, PRO1 301-450, PRO2 451-600, Master1
-- 601-750, Master2 751-900, GM1 901-1050, GM2 1051-1200, Legendary1
-- 1201-1350, Legendary2 1351-1500, Titans 1501+.
--
-- IMPORTANT: this CASE chain is duplicated (current_rank + peak_rank)
-- inside approve_ro3_match itself — it is NOT read from mmr_engine.py.
-- services/mmr_engine.py's derive_rank() must be kept in sync with this
-- function by hand; there is no single source of truth at the code
-- level, only convention + comments on both sides. See DECISIONS.md.
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
-- 3. Career-stat recompute function — idempotent, one player at a time.
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
-- delta came from the winning side (position deltas are always positive
-- for the winning team, negative for the losing team; the MVP +5 only
-- ever adds on top and never flips the sign on its own — position 5 on
-- a loss is -9+5=-4, still negative). Every completed match therefore
-- contributes exactly 3 to wins+losses combined. This sidesteps the
-- "how do 3 rounds collapse into 1 match result" question in
-- DECISIONS.md entirely for record-keeping purposes — that question
-- still matters for anything that wants a single per-match verdict,
-- just not for this card.
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
    -- Round-level win/loss, summed across every completed match. Every
    -- match contributes exactly 3 rows here (one per round), so this is
    -- a straight count, not a per-match majority calculation.
    select count(*) filter (where mrr.mmr_delta > 0),
           count(*) filter (where mrr.mmr_delta <= 0)
    into v_wins, v_losses
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.player_id = p_player_id and m.status = 'completed';

    -- total_matches stays match-level (distinct completed matches the
    -- player took part in) — this is what "Total Matches" on the card
    -- means, separate from the round-level wins/losses above.
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
