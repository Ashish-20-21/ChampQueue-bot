-- migration_021_fix_achievement_null_season_duplicates.sql
-- Fix 2026-08-21: player_achievements' unique(player_id, achievement_id,
-- season_id) constraint does NOT reliably prevent duplicates for
-- permanent achievements, which always use season_id = NULL.
--
-- Bug: in Postgres, NULL is never equal to another NULL for uniqueness
-- purposes. A plain `unique(a, b, c)` constraint on a nullable column
-- allows unlimited rows with the same (a, b) and c = NULL, since two
-- NULLs never "conflict". check_and_grant_achievements()'s `on conflict
-- (player_id, achievement_id, season_id) do nothing` (migration_020)
-- therefore silently failed to dedupe every time it was re-run for a
-- permanent badge -- confirmed live 2026-08-21: running the backfill
-- multiple times (once per threshold recalibration pass) produced
-- duplicate player_achievements rows for the same permanent badge,
-- visible in the old /achievements list rendering as e.g. "Initiator"
-- appearing twice for the same player.
--
-- Fix: two unique PARTIAL indexes instead of one plain unique
-- constraint -- one for season_id IS NULL rows (permanent achievements),
-- one for season_id IS NOT NULL rows (seasonal achievements, if/when
-- those are ever actually granted). A partial index's WHERE clause
-- means every NULL-season row is compared against every other NULL-
-- season row for the SAME (player_id, achievement_id) pair, which is
-- exactly the dedup guarantee the original constraint was assumed to
-- provide but didn't.

-- Step 1: clean up existing duplicates before adding the new indexes
-- (the new indexes can't be created while duplicate rows still exist).
-- Keeps the earliest (lowest id) row per (player_id, achievement_id,
-- season_id), removes the rest. Uses IS NOT DISTINCT FROM (Postgres's
-- null-safe equality) so NULL season_id rows are correctly grouped
-- together for this cleanup, unlike the broken constraint they came
-- from.
delete from player_achievements a
using player_achievements b
where a.id > b.id
  and a.player_id = b.player_id
  and a.achievement_id = b.achievement_id
  and a.season_id is not distinct from b.season_id;

-- Step 2: drop the old, ineffective plain unique constraint. Name
-- confirmed directly against information_schema.table_constraints on
-- 2026-08-21 (Postgres's standard auto-generated name for an inline,
-- unnamed unique(...) declaration -- schema.sql never gave this
-- constraint an explicit name) rather than assumed/guessed.
alter table player_achievements
    drop constraint if exists player_achievements_player_id_achievement_id_season_id_key;

-- Step 3: add the two partial unique indexes that actually enforce
-- "one grant per player per achievement" correctly for both permanent
-- (season_id null) and seasonal (season_id set) achievements.
create unique index if not exists player_achievements_unique_permanent
    on player_achievements (player_id, achievement_id)
    where season_id is null;

create unique index if not exists player_achievements_unique_seasonal
    on player_achievements (player_id, achievement_id, season_id)
    where season_id is not null;

-- ============================================================
-- IMPORTANT: check_and_grant_achievements()'s on conflict clause
-- (migration_020) currently reads:
--   on conflict (player_id, achievement_id, season_id) do nothing
-- This must be updated to match the new partial indexes -- a plain
-- on conflict (a, b, c) can no longer resolve against two separate
-- partial unique indexes. See migration_022 (companion fix, applied
-- right after this one) for the corrected function body.
-- ============================================================

-- ============================================================
-- VERIFICATION: confirms zero duplicate (player_id, achievement_id)
-- pairs remain among permanent (season_id null) achievements.
-- ============================================================
-- select player_id, achievement_id, count(*)
-- from player_achievements
-- where season_id is null
-- group by player_id, achievement_id
-- having count(*) > 1;
-- (expected: 0 rows)
