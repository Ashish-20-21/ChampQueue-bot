-- migration_023_season_activation.sql
--
-- Season 1 end-of-season prep, part 1: activate season_id tracking.
--
-- Context: `seasons`, `hall_of_fame`, and `player_achievements` already
-- exist in schema.sql and are season-aware. `matches.season_id` exists and
-- `db.py::create_match()` already accepts a season_id param — but the only
-- call site (cogs/queue.py::_start_match_flow) has never passed it. Every
-- match created to date has season_id = NULL. This migration:
--   1. Adds a short human-readable `code` column to `seasons` (independent
--      of `name`), so reports/hall-of-fame posts can reference "S1-0826"
--      without overloading the season's display name.
--   2. Ensures exactly one season is active and tagged with that code.
--   3. Backfills every existing NULL matches.season_id to point at it.
--
-- Per project convention: run the STEP 0 verify query first and read the
-- result before running the write steps. Do not skip it.

-- ============================================================
-- STEP 0 — VERIFY CURRENT STATE (run this first, read the output)
-- ============================================================
select id, name, start_date, end_date, is_active
from seasons
order by id;

select count(*) as null_season_matches
from matches
where season_id is null;

select count(*) as total_matches
from matches;

-- If STEP 0's first query returns more than one row with is_active = true,
-- STOP and resolve that by hand before continuing — get_active_season() in
-- db.py does `.eq("is_active", true)` with no ordering/limit, so more than
-- one active row makes season lookups non-deterministic. This should not
-- happen from schema.sql's seed logic alone, but verify.

-- ============================================================
-- STEP 1 — ENSURE AN ACTIVE SEASON EXISTS, ADD + SET code
-- ============================================================
alter table seasons add column if not exists code text;
create unique index if not exists idx_seasons_code on seasons(code) where code is not null;

-- Only inserts if no season is currently active (mirrors schema.sql's own
-- seed logic — this is the same idempotent guard, safe to re-run).
insert into seasons (name, code, is_active)
select 'Season 1', 'S1-0826', true
where not exists (select 1 from seasons where is_active = true);

-- If a season was already active (the schema.sql seed already ran on prod
-- at some point), it won't have gotten a code from the insert above — set
-- it explicitly. Only touches rows where code is currently unset, so this
-- is safe to re-run.
update seasons
set code = 'S1-0826'
where is_active = true
  and code is null;

-- ============================================================
-- STEP 2 — BACKFILL EXISTING MATCHES
-- ============================================================
update matches
set season_id = (select id from seasons where is_active = true limit 1)
where season_id is null;

-- ============================================================
-- STEP 3 — VERIFY AFTER WRITE (run this, confirm it matches expectations)
-- ============================================================
select id, name, code, is_active from seasons order by id;

select season_id, count(*) as match_count
from matches
group by season_id
order by season_id nulls first;
-- Expect: zero rows (or zero count) with season_id null after this point.
