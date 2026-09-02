-- migration_028_season_1_participation_badge.sql
--
-- "Played Season 1" badge — a participation marker, not a career
-- threshold (see utils/embeds.py's _SEASONAL_BADGES comment for the
-- distinction). code convention going forward: "played_s{N}" per season.
--
-- Uses achievements.category='seasonal', which the schema's own check
-- constraint already allows (confirmed against schema.sql — 'general',
-- 'hardpoint', 'streak', 'seasonal' were always valid values, this is
-- the first row to actually use 'seasonal').
--
-- Grant scope: every player with >=1 completed Season 1 match (real
-- participation, not just registering and never playing). Uses the
-- existing player_achievements unique constraint
-- (player_id, achievement_id, season_id) as the safety net against
-- double-granting — plain insert...select with on conflict do nothing,
-- safe to re-run.

-- ============================================================
-- STEP 0 — VERIFY CURRENT STATE (run first, read the output)
-- ============================================================
select id, code, name, category from achievements where code = 'played_s1';
-- Expect zero rows — this badge doesn't exist yet.

select count(distinct mp.player_id) as players_who_played_s1
from match_players mp
join matches m on m.id = mp.match_id
where m.season_id = 1
  and m.status = 'completed';
-- Sanity check against the number this migration is about to grant to.

-- ============================================================
-- STEP 1 — CREATE THE ACHIEVEMENT
-- ============================================================
insert into achievements (code, name, description, category)
values ('played_s1', 'Urising Season 1', 'Played in Season 1', 'seasonal')
on conflict (code) do nothing;

-- ============================================================
-- STEP 1.5 — SCHEMA DRIFT FIX (discovered live during this migration)
-- ============================================================
-- schema.sql declares `unique (player_id, achievement_id, season_id)`
-- on player_achievements, but prod's actual table has NO such
-- constraint — confirmed via pg_constraint: only the primary key and
-- 3 foreign keys exist, no unique constraint at all. This means the
-- reference schema and the live table have drifted apart at some
-- point. Adding it now, both so Step 2 below has a real conflict
-- target to use, and so this table finally matches its own documented
-- design going forward (protects against future double-grants from
-- any other code path, not just this migration).
--
-- Safe to add now: player_achievements is currently empty of any
-- 'played_s1' rows (this is the first grant), and no other seasonal
-- achievement exists yet either, so there's no pre-existing duplicate
-- data that would make this ALTER TABLE fail.
alter table player_achievements
    add constraint player_achievements_player_achievement_season_key
    unique (player_id, achievement_id, season_id);

-- ============================================================
-- STEP 2 — GRANT TO EVERY PLAYER WHO PLAYED >=1 SEASON 1 MATCH
-- ============================================================
insert into player_achievements (player_id, achievement_id, season_id)
select distinct mp.player_id,
       (select id from achievements where code = 'played_s1'),
       1
from match_players mp
join matches m on m.id = mp.match_id
where m.season_id = 1
  and m.status = 'completed'
on conflict (player_id, achievement_id, season_id) do nothing;

-- ============================================================
-- STEP 3 — VERIFY AFTER
-- ============================================================
select count(*) as players_with_s1_badge
from player_achievements pa
join achievements a on a.id = pa.achievement_id
where a.code = 'played_s1';
-- Expect this to match "players_who_played_s1" from Step 0.
