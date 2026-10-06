-- migration_041_impact_crown.sql
-- Branch: unified-region-ro1
--
-- What changes: the +5 MMR bonus moves from the MVP tag to the Impact
-- CROWN holder (top Impact player of each team, any row 1-5).
--
-- match_round_results gets two columns:
--   is_crown  boolean NULL  — the crown holder, read from the screenshot or
--                             set by an admin. NULL = never recorded (every
--                             match before this migration). Not backfilled:
--                             we never read crowns before, so we don't guess.
--   bonus_5   boolean       — "this row received the +5". The ONLY flag the
--                             win/loss maths reads. Backfilled from is_mvp,
--                             because that is exactly who got the +5 before.
--                             Without this backfill a losing MVP's -3+5=+2
--                             would start counting as a WIN.
--   is_mvp    (unchanged)   — stat only from now on: career / Hall of Fame /
--                             weekly MVP counts keep reading it untouched.
--
-- CHECK (is_crown is null or bonus_5 = is_crown): every write path (bot,
-- /admin-correct-round, /admin-enter-result) must keep the two in step, or
-- the write fails loudly instead of corrupting wins/losses.
--
-- matches.crown_override jsonb: an admin's crown pick when the crown was
-- hidden/unclear on the screenshot, e.g. {"A": {"position": 2, "by": "<id>",
-- "at": "<ts>"}}. The raw OCR record in match_screenshots is never edited.
--
-- Functions rewritten (bodies copied from the LIVE definitions on
-- 2026-10-06; the ONLY change in each is is_mvp -> bonus_5 in the win/loss
-- check, plus the insert columns in replace_match_round_data):
--   replace_match_round_data, recompute_player_career_stats,
--   recompute_season_points_for_match, update_season_points_for_match,
--   current_season_stats.
-- NOT touched (they only COUNT MVPs, which stays on is_mvp): hof_most_mvps,
--   weekly_leaders, season_recap_stats (both overloads), check_and_grant_
--   achievements, region_leaderboard. approve_match applies the stored
--   mmr_delta and never reads is_mvp.
--
-- Rollout safety: replace_match_round_data falls back to is_mvp when a
-- caller doesn't send bonus_5 — so if this migration runs BEFORE the new
-- bot code is deployed, matches submitted by the old code still get the
-- right bonus_5. Run it in the nightly maintenance window and deploy the
-- bot right after; avoid /admin-correct-round in between (the old command
-- updates is_mvp without bonus_5).
--
-- Run on the TEST Supabase first. Whole file is one transaction.

begin;

-- ── 1. columns ──────────────────────────────────────────────────────
alter table match_round_results
    add column if not exists is_crown boolean,
    add column if not exists bonus_5  boolean not null default false;

alter table matches
    add column if not exists crown_override jsonb;

-- ── 2. backfill: the +5 went to the MVP tag holder before today ──────
update match_round_results
set bonus_5 = true
where is_mvp = true and bonus_5 = false;

-- ── 3. invariant ────────────────────────────────────────────────────
alter table match_round_results
    drop constraint if exists match_round_results_crown_bonus_chk;
alter table match_round_results
    add constraint match_round_results_crown_bonus_chk
    check (is_crown is null or bonus_5 = is_crown);

-- ── 4. write path ───────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.replace_match_round_data(p_match_id bigint, p_round_number integer, p_round_results jsonb, p_player_stats jsonb)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
begin
    -- match_round_results: same delete-then-insert shape as the old
    -- Python _replace_match_round_results, just inside one transaction
    -- instead of two separate HTTP round trips.
    delete from match_round_results
    where match_id = p_match_id and round_number = p_round_number;

    if p_round_results is not null and jsonb_array_length(p_round_results) > 0 then
        -- migration_041: is_crown / bonus_5 added. bonus_5 falls back to
        -- is_crown, then is_mvp, so a caller still on the old payload
        -- (is_mvp only) keeps win/loss correct during the rollout.
        insert into match_round_results (match_id, round_number, player_id, position, is_mvp, is_crown, bonus_5, mmr_delta, team)
        select p_match_id, p_round_number,
               (row->>'player_id')::bigint,
               (row->>'position')::integer,
               coalesce((row->>'is_mvp')::boolean, false),
               (row->>'is_crown')::boolean,
               coalesce((row->>'bonus_5')::boolean, (row->>'is_crown')::boolean, (row->>'is_mvp')::boolean, false),
               (row->>'mmr_delta')::integer,
               row->>'team'
        from jsonb_array_elements(p_round_results) as row;
    end if;

    -- match_player_stats: same shape, same transaction. Failing partway
    -- through either table's write now rolls back BOTH tables for this
    -- round, not just the one call in flight.
    delete from match_player_stats
    where match_id = p_match_id and round_number = p_round_number;

    if p_player_stats is not null and jsonb_array_length(p_player_stats) > 0 then
        insert into match_player_stats (match_id, round_number, player_id, kills, deaths, assists, damage, hill_time, impact, score)
        select p_match_id, p_round_number,
               (row->>'player_id')::bigint,
               (row->>'kills')::integer,
               (row->>'deaths')::integer,
               (row->>'assists')::integer,
               nullif(row->>'damage', '')::integer,
               (row->>'hill_time')::numeric(6,2),
               nullif(row->>'impact', '')::numeric(6,2),
               (row->>'score')::integer
        from jsonb_array_elements(p_player_stats) as row;
    end if;
end;
$function$;

-- ── 5. career stats: wins/losses strip bonus_5; mvp_count stays is_mvp ─
CREATE OR REPLACE FUNCTION public.recompute_player_career_stats(p_player_id bigint)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
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
    -- total_matches is now count(*) on the SAME row set wins/losses are
    -- derived from (was count(distinct mrr.match_id) in a separate query
    -- -- see migration comment above for why that could disagree with
    -- wins+losses for players with RO3-era history). Computed together
    -- with wins/losses in one query so the three numbers can never
    -- independently drift out of sync again.
    -- migration_041: the +5 is stripped via bonus_5 (crown now, MVP tag on
    -- old rows), not is_mvp.
    select count(*),
           count(*) filter (where (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) > 0),
           count(*) filter (where (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) <= 0)
    into v_total_matches, v_wins, v_losses
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
    set total_matches = coalesce(v_total_matches, 0),
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
$function$;

-- ── 6. season points: win/loss strips bonus_5 ───────────────────────
CREATE OR REPLACE FUNCTION public.recompute_season_points_for_match(p_match_id bigint)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
declare
    v_season_id bigint;
begin
    select season_id into v_season_id
    from matches
    where id = p_match_id;

    if v_season_id is null then
        return;
    end if;

    delete from season_point_events
    where match_id = p_match_id and season_id = v_season_id;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        v_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) > 0 then
                case
                    when gst.tier is null then 5
                    when gst.tier = 'premium'
                         and m.completed_at < gst.shield_starts_at + interval '24 hours'
                        then 50
                    else 10
                end
            else
                case when gst.tier is not null then 0 else -3 end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) <= 0
                 and gst.tier is not null
                then true
            else false
        end
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    left join lateral get_active_shield_tier(mrr.player_id, v_season_id) gst on true
    where mrr.match_id = p_match_id;

    perform recompute_player_season_points(spe.player_id, v_season_id)
    from (select distinct player_id from season_point_events where match_id = p_match_id and season_id = v_season_id) spe;
end;
$function$;

CREATE OR REPLACE FUNCTION public.update_season_points_for_match(p_match_id bigint, p_season_id bigint)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
declare
    v_already_locked boolean;
begin
    select is_season_points_locked(p_season_id) into v_already_locked;
    if v_already_locked then
        return;
    end if;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        p_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) > 0 then
                -- WON. Tier-aware bonus — win_sp/day1_win_sp values
                -- documented in config.SHIELD_TIERS; duplicated here
                -- as literals because SQL has no reach into config.py
                -- (same "duplicated across N copies" class as the
                -- rank-tier table — see engineering-rules' Known
                -- landmines). If either tier's win amounts change in
                -- config.py, this CASE must be updated to match.
                case
                    when gst.tier is null then 5                                    -- no shield
                    when gst.tier = 'premium'
                         and now() < gst.shield_starts_at + interval '24 hours'
                        then 50                                                      -- premium, day 1
                    else 10                                                          -- normal / 2x_normal / premium day 2-3
                end
            else
                -- LOST. All three tiers give 0 on loss, so this stays
                -- a simple "shielded at all?" check.
                case when gst.tier is not null then 0 else -3 end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.bonus_5 then 5 else 0 end)) <= 0
                 and gst.tier is not null
                then true
            else false
        end
    from match_round_results mrr
    left join lateral get_active_shield_tier(mrr.player_id, p_season_id) gst on true
    where mrr.match_id = p_match_id
    on conflict (season_id, player_id, match_id) do update
        set delta = excluded.delta,
            was_shielded = excluded.was_shielded;

    insert into season_points (season_id, player_id, points, updated_at)
    select
        p_season_id,
        spe.player_id,
        greatest(0, spe.delta),
        now()
    from season_point_events spe
    where spe.match_id = p_match_id and spe.season_id = p_season_id
    on conflict (season_id, player_id) do update
        set points = greatest(0, season_points.points + (
                select spe2.delta
                from season_point_events spe2
                where spe2.match_id = p_match_id
                  and spe2.season_id = p_season_id
                  and spe2.player_id = season_points.player_id
            )),
            updated_at = now();

    -- Pool-unlock check only — no locking happens here anymore.
    perform check_and_unlock_pool(p_season_id);
end;
$function$;

-- ── 7. /cs-stats: wins/losses strip bonus_5; mvps stays is_mvp ──────
CREATE OR REPLACE FUNCTION public.current_season_stats(p_player_id bigint, p_season_id bigint)
 RETURNS TABLE(matches bigint, total_kills bigint, total_deaths bigint, avg_kills numeric, avg_hill_time numeric, wins bigint, losses bigint, mvps bigint)
 LANGUAGE plpgsql
 STABLE
AS $function$
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
        SELECT mrr.mmr_delta, mrr.is_mvp, mrr.bonus_5
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
        COALESCE((SELECT COUNT(*) FROM results WHERE (mmr_delta - CASE WHEN bonus_5 THEN 5 ELSE 0 END) > 0), 0),
        COALESCE((SELECT COUNT(*) FROM results WHERE (mmr_delta - CASE WHEN bonus_5 THEN 5 ELSE 0 END) <= 0), 0),
        COALESCE((SELECT COUNT(*) FROM results WHERE is_mvp), 0);
END;
$function$;

commit;

-- ── Verify after running (expect 0 rows / 0 counts) ─────────────────
-- 1. every old +5 row got bonus_5:
--    select count(*) from match_round_results where is_mvp and not bonus_5;
-- 2. no crown/bonus drift:
--    select count(*) from match_round_results where is_crown is not null and bonus_5 <> is_crown;
-- 3. wins/losses unchanged for everyone (run BEFORE and AFTER, compare):
--    select sum(wins), sum(losses) from players;
--    (then: select recompute_player_career_stats_bulk(array(select id from players)); and compare again)
