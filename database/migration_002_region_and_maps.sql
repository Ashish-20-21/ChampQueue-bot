-- ============================================================
-- MIGRATION 002: map announcement (no vote) + region-scoped queue
-- ------------------------------------------------------------
-- Run after schema.sql and migration_ro3_verification.sql. Purely
-- additive.
-- ============================================================

-- 1. Map pool: 3 maps picked by matchmaking.pick_map_candidates() and
--    announced (not voted on). map_pool[0] = round 1's map, [1] = round 2,
--    [2] = round 3. Keep the existing `matches.map` column as a legacy
--    single-value field — stop writing to it, match.py should read/write
--    map_pool instead.
alter table matches add column if not exists map_pool text[];

-- 2. Region is already a column on `players` (added in the original
--    schema.sql) — this was never used as a matchmaking filter. No new
--    column needed, just an index, since every region-scoped queue query
--    will filter/join on it.
create index if not exists idx_players_region on players(region);

-- 3. Constrain region to the two supported matchmaking pools (confirmed
--    with the team: East / West, ping-separated, no other regions at
--    launch). Casing matches what's used everywhere else in code/config —
--    keep it consistent if you ever add a region.
alter table players drop constraint if exists players_region_check;
alter table players add constraint players_region_check check (
    region in ('East', 'West')
);
