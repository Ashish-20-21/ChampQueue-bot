-- migration_020_achievement_badges.sql
-- Adds the permanent career-badge achievements set (2026-08-21 design
-- session) and rebuilds the achievement pipeline on top of the
-- RO1-correct player stat columns fixed in migration_019, replacing the
-- Python-side services/stats_engine.py path (confirmed dead code, zero
-- live callers -- see that file's own docstring). The check-and-grant
-- logic moves into Postgres so it can be called from the same
-- recompute_player_career_stats() call site already wired into
-- cogs/match.py's approval flow -- one more RPC call per player, same
-- trigger point, no new hook needed elsewhere.
--
-- Two distinct badge types, per the 2026-08-21 design discussion:
--   1. PERMANENT badges -- earn once via player_achievements (existing
--      table), kept forever even if the underlying stat later changes
--      (e.g. KD dips below 1.0 after earning "Positive KD").
--   2. LIVE TITLES -- NOT stored anywhere. Computed fresh every time
--      /achievements is viewed, via live_player_titles() below. Whoever
--      currently leads a category holds the title; it moves the instant
--      someone overtakes them -- same mechanic as the existing weekly
--      badges (weekly_leaders()), just not time-windowed.

-- ============================================================
-- SEED: permanent career-badge achievements
-- ============================================================
insert into achievements (code, name, description, category) values
    ('first_blood',    'First Blood',    '250 career kills', 'general'),
    ('kill_slayer',    'Kill Slayer',    '750 career kills', 'general'),
    ('death_dealer',   'Death Dealer',   '1500 career kills', 'general'),
    ('sharpshooter',   'Sharpshooter',   '3000 career kills', 'general'),
    ('mvp_king',       'MVP King',       '20 career MVPs', 'general'),
    ('mvp_legend',     'MVP Legend',     '30 career MVPs', 'general'),
    ('initiator',      'Initiator',      'Played your first match', 'general'),
    ('pacekeeper',     'Pacekeeper',     '10 matches played', 'general'),
    ('grinder',        'Grinder',        '30 matches played', 'general'),
    ('veteran',        'Veteran',        '50 matches played', 'general'),
    ('centurion',      'Centurion',      '100+ matches played', 'general'),
    ('positive_kd',    'Positive KD',    'Career KD above 1.0', 'general'),
    ('rank_pro',       'PRO',            'Reached PRO1 rank', 'general'),
    ('rank_master',    'Master',         'Reached Master1 rank', 'general'),
    ('rank_grandmaster','Grandmaster',   'Reached Grandmaster1 rank', 'general'),
    ('rank_legendary', 'Legendary',      'Reached Legendary1 rank', 'general'),
    ('rank_titan',     'Titan',          'Reached Titans rank', 'general')
on conflict (code) do nothing;

-- win_streak_10, positive_kd_streak, mvp_streak already exist from the
-- original schema seed (see schema.sql) -- re-used as-is, not
-- duplicated here. Renamed nothing: those codes are unchanged so any
-- already-earned rows (if the old dead pipeline somehow ever ran) stay
-- valid.

-- ============================================================
-- check_and_grant_achievements: checks one player's CURRENT stat
-- columns (already fresh -- always called right after
-- recompute_player_career_stats() for the same player) against every
-- permanent-badge threshold and grants any newly-qualified ones.
-- Idempotent: grant_achievement-equivalent insert below uses
-- on conflict do nothing against the existing unique(player_id,
-- achievement_id, season_id) constraint, so calling this on an
-- already-earned badge is a safe no-op, not a duplicate/error.
-- ============================================================
create or replace function check_and_grant_achievements(p_player_id bigint)
returns table (granted_code text)
language plpgsql
as $$
declare
    v_player players%rowtype;
    v_total_kills numeric;
    v_kd numeric;
    v_rank text;
    v_code text;
begin
    select * into v_player from players where id = p_player_id;
    if not found then
        return;
    end if;

    -- avg_kills is a per-round average, not a running total -- multiply
    -- back out by total_matches (== total rounds counted, per
    -- migration_019) for a career kill count. Rounded since avg_kills
    -- itself is already rounded to 2dp in recompute_player_career_stats,
    -- so this is an approximation, not an exact running counter -- a
    -- true running total would need its own column, out of scope here.
    v_total_kills := round(v_player.avg_kills * v_player.total_matches);
    v_kd := case when v_player.avg_deaths > 0 then v_player.avg_kills / v_player.avg_deaths else v_player.avg_kills end;

    -- current_rank is written by approve_match at approval time (see
    -- migration_015_ro1.sql) -- reliable signal for "reached tier X ever"
    -- ONLY if we check against peak, not current (current can drop back
    -- down after a loss). Using peak_rank instead, which is only ever
    -- updated upward (see services/stats_engine.py's update_rank logic,
    -- mirrored here) -- so a rank badge earned once is never lost even
    -- if MMR later falls.
    v_rank := v_player.peak_rank;

    for v_code in
        select code from (values
            ('first_blood',     v_total_kills >= 250),
            ('kill_slayer',     v_total_kills >= 750),
            ('death_dealer',    v_total_kills >= 1500),
            ('sharpshooter',    v_total_kills >= 3000),
            ('mvp_king',        v_player.mvp_count >= 20),
            ('mvp_legend',      v_player.mvp_count >= 30),
            ('initiator',       v_player.total_matches >= 1),
            ('pacekeeper',      v_player.total_matches >= 10),
            ('grinder',         v_player.total_matches >= 30),
            ('veteran',         v_player.total_matches >= 50),
            ('centurion',       v_player.total_matches >= 100),
            ('positive_kd',     v_kd > 1.0),
            ('rank_pro',        v_rank in ('PRO1','PRO2','Master1','Master2','Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_master',     v_rank in ('Master1','Master2','Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_grandmaster',v_rank in ('Grandmaster1','Grandmaster2','Legendary1','Legendary2','Titans')),
            ('rank_legendary',  v_rank in ('Legendary1','Legendary2','Titans')),
            ('rank_titan',      v_rank = 'Titans')
        ) as checks(code, qualifies)
        where qualifies
    loop
        insert into player_achievements (player_id, achievement_id, season_id)
        select p_player_id, a.id, null
        from achievements a
        where a.code = v_code
        on conflict (player_id, achievement_id, season_id) do nothing
        returning v_code into granted_code;
        if granted_code is not null then
            return next;
        end if;
    end loop;
end;
$$;

-- ============================================================
-- live_player_titles: returns the LIVE (unstored, always-current)
-- titles a specific player currently holds. Computed fresh on every
-- call -- no grant, no storage, moves instantly when standings change.
-- Mirrors weekly_leaders()'s "whoever's on top right now" pattern.
-- ============================================================
create or replace function live_player_titles(p_player_id bigint)
returns table (title_code text, title_name text)
language plpgsql
as $$
declare
    v_rank_position integer;
    v_top_mvp_id bigint;
    v_top_matches_id bigint;
    v_top_kd_id bigint;
    v_top_kd numeric := -1;
    r record;
begin
    -- Ladder position (1-indexed, MMR descending) -- only among players
    -- with at least 1 match, same "real player" filter as the
    -- leaderboard uses elsewhere.
    select rank_pos into v_rank_position
    from (
        select id, row_number() over (order by mmr desc) as rank_pos
        from players
        where total_matches > 0
    ) ranked
    where ranked.id = p_player_id;

    if v_rank_position = 1 then
        title_code := 'top_of_ladder'; title_name := 'Top of the Ladder'; return next;
    elsif v_rank_position between 2 and 10 then
        title_code := 'top_10'; title_name := 'Top 10'; return next;
    elsif v_rank_position between 11 and 50 then
        title_code := 'top_50'; title_name := 'Top 50'; return next;
    end if;

    -- Most MVPs ever (ties: lowest id wins, i.e. earliest player to reach
    -- that count -- arbitrary but deterministic tiebreak, same shape
    -- Postgres would pick implicitly with order by ... limit 1).
    select id into v_top_mvp_id from players where total_matches > 0 order by mvp_count desc, id asc limit 1;
    if v_top_mvp_id = p_player_id then
        title_code := 'most_mvps_ever'; title_name := 'Most MVPs Ever'; return next;
    end if;

    select id into v_top_matches_id from players where total_matches > 0 order by total_matches desc, id asc limit 1;
    if v_top_matches_id = p_player_id then
        title_code := 'most_matches_ever'; title_name := 'Most Matches Played'; return next;
    end if;

    -- Highest KD: computed in Postgres (avg_kills/avg_deaths), same
    -- guard against div-by-zero as the Python-side player_stats_card.
    select id into v_top_kd_id
    from (
        select id, case when avg_deaths > 0 then avg_kills / avg_deaths else avg_kills end as kd
        from players
        where total_matches > 0
    ) ranked
    order by kd desc, id asc
    limit 1;
    if v_top_kd_id = p_player_id then
        title_code := 'highest_kd_ever'; title_name := 'Highest KD'; return next;
    end if;

    return;
end;
$$;

-- ============================================================
-- BACKFILL: run once after applying the function above, so every
-- EXISTING player who already qualifies for a permanent badge gets it
-- immediately, instead of waiting for their next match to trigger the
-- live hook. Live titles need no backfill -- they're never stored,
-- always computed fresh. Safe to run multiple times (idempotent, same
-- on-conflict-do-nothing as check_and_grant_achievements itself).
-- ============================================================
-- select check_and_grant_achievements(id) from players where total_matches > 0;

-- ============================================================
-- VERIFICATION (optional, run after backfill): spot-check counts per
-- badge to sanity-check the thresholds look reasonable for your
-- playerbase before relying on it live.
-- ============================================================
-- select a.code, a.name, count(pa.id) as earned_count
-- from achievements a
-- left join player_achievements pa on pa.achievement_id = a.id
-- group by a.code, a.name
-- order by earned_count desc;
