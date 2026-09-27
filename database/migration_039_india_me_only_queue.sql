-- ============================================================
-- MIGRATION 039: Add INDIA_ME_ONLY queue_key
-- ------------------------------------------------------------
-- Context (2026-09-27): the JAPAN physical queue has no active
-- players. Meanwhile some India/ME players want a queue restricted to
-- India/ME-tagged players only, to route around India-only-queue
-- complaints. JAPAN's queue_key is deliberately NOT repurposed/renamed
-- (that would silently relabel its historical queue_entries/matches
-- rows) — it stays as-is, just hidden from slash-command choice lists
-- at the application layer (config.QUEUES[...]["active"] = False).
-- This migration only WIDENS the queue_key check constraints to also
-- permit the new value, same pattern as migration_010: existing rows
-- (including JAPAN ones) are left untouched, nothing is backfilled or
-- replaced.
--
-- Note: players.region / players_region_check is NOT touched —
-- that one really is decoupled (informational-only registration
-- label, per migration_010).
--
-- matches.region / matches_region_check IS touched, correcting an
-- error in the first cut of this migration. cogs/queue.py:839 writes
-- `"region": queue_key` into the matches row at creation time —
-- migration_010 deliberately keeps matches.region and matches.queue_key
-- in sync ("harmless to keep in sync with players", its own comment).
-- Missing this caused a live 23514 failure on matches_region_check
-- the first time a real INDIA_ME_ONLY match tried to form (every
-- player correctly rolled back by queue_mark_waiting, no stuck rows —
-- but the match itself never got created). Widening this constraint
-- too, same pattern as everything else here.
-- ============================================================

alter table queue_entries drop constraint if exists queue_entries_queue_key_check;
alter table queue_entries add constraint queue_entries_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN', 'INDIA_ME_ONLY')
);

alter table matches drop constraint if exists matches_queue_key_check;
alter table matches add constraint matches_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN', 'INDIA_ME_ONLY')
);

alter table matches drop constraint if exists matches_region_check;
alter table matches add constraint matches_region_check check (
    region in ('East', 'West', 'EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN', 'INDIA_ME_ONLY')
);