-- migration_037_host_replace_counter.sql
-- Adds a counter so /host-replace-player can enforce a per-match limit.
-- Run this BEFORE deploying the code that uses it.

ALTER TABLE matches
    ADD COLUMN IF NOT EXISTS host_replacements_used integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN matches.host_replacements_used IS
    'Number of host-initiated player replacements in this match. Enforced by /host-replace-player against HOST_REPLACE_LIMIT.';
