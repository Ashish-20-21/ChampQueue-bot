-- ============================================================
-- MIGRATION: fix queue_entries unique constraint
-- ------------------------------------------------------------
-- Bug found during local testing: the original constraint was
-- unique (player_id, status), which blocks a player from EVER having
-- more than one 'left' (or 'matched') row, forever. In real use, a
-- player leaves the queue many times over a season — the second time
-- any player calls /leave (or clicks Leave Queue), queue_leave() crashes
-- with a duplicate-key error (23505), because a (player_id, 'left') row
-- already exists from the first time they ever left.
--
-- What actually needs to be unique: a player should never have two
-- SIMULTANEOUS 'waiting' rows (i.e. can't be in the queue twice at once).
-- Historical 'left'/'matched' rows should be allowed to accumulate freely
-- — that's just queue history, not a constraint violation.
-- ============================================================

-- 1. Drop the old, too-broad constraint.
alter table queue_entries drop constraint if exists queue_entries_player_id_status_key;

-- 2. Partial unique index: only enforces uniqueness among 'waiting' rows.
--    A player can have unlimited 'left'/'matched' rows in their history,
--    but at most one 'waiting' row at any given time.
create unique index if not exists idx_queue_entries_one_waiting_per_player
    on queue_entries (player_id)
    where (status = 'waiting');
