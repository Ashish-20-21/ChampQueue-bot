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
-- Note: this does NOT touch players.region / players_region_check or
-- matches.region / matches_region_check. INDIA_ME_ONLY is a queue_key
-- only, not a registration region — region and queue_key stay
-- decoupled exactly as migration_010 established.
-- ============================================================

alter table queue_entries drop constraint if exists queue_entries_queue_key_check;
alter table queue_entries add constraint queue_entries_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN', 'INDIA_ME_ONLY')
);

alter table matches drop constraint if exists matches_queue_key_check;
alter table matches add constraint matches_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN', 'INDIA_ME_ONLY')
);
