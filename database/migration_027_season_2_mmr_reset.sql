-- migration_027_season_2_mmr_reset.sql
--
-- Reset every player's live MMR to 200 (the schema's own default for a
-- brand-new player, confirmed mmr_engine.derive_rank(200) == 'Elite1')
-- for the Season 2 fresh start.
--
-- Timing note (why this is safe to run with a match currently in
-- flight): approve_match() (migration_015_ro1.sql) reads players.mmr
-- LIVE at approval time — `p.mmr + d.total_delta` — not a value
-- captured when the match started. So any match still sitting at
-- awaiting_result / pending_verification / awaiting_review right now
-- will correctly apply its win/loss delta on top of the fresh 200
-- baseline once it's approved, which is exactly "this match counts as
-- a Season 2 match" — no special handling needed, no need to wait for
-- it to finish first.
--
-- Scope decision: ONLY players.mmr and players.current_rank reset here.
-- Deliberately NOT touched:
--   - peak_mmr / peak_rank — these are a player's all-time career best,
--     not a season-scoped stat; resetting them would erase a real
--     achievement, not start a new season.
--   - Everything else on players (career K/D, total_matches, avg_kills,
--     etc.) — those are lifetime stats, same reasoning as peak_mmr.
--     If Season 2 wants season-scoped career stats shown separately
--     from lifetime ones, that's a bigger, different feature (a
--     season_id-scoped stats view) — not something to bolt onto a
--     quick MMR reset.
--   - match_players.mmr_before/mmr_after/mmr_change on existing rows —
--     historical record of what actually happened in Season 1, must
--     stay exactly as-is for Hall of Fame / audit purposes.
--
-- Scoped to status='approved' players only — pending/rejected
-- registrations have no meaningful "season MMR" to reset.

-- ============================================================
-- STEP 0 — VERIFY CURRENT STATE (run first, read the output)
-- ============================================================
select count(*) as approved_players,
       round(avg(mmr), 1) as avg_mmr_before,
       max(mmr) as max_mmr_before,
       min(mmr) as min_mmr_before
from players
where status = 'approved';

-- Sanity check: how many players are ALREADY at 200 (so the reset is a
-- no-op for them)? Not required reading, just useful context.
select count(*) as already_at_200
from players
where status = 'approved' and mmr = 200;

-- ============================================================
-- STEP 1 — RESET
-- ============================================================
-- NOTE: current_division was dropped from players in migration_016
-- (2026-08-15) — confirmed via schema.sql, not included here.
update players
set mmr = 200,
    current_rank = 'Elite1',
    updated_at = now()
where status = 'approved';

-- ============================================================
-- STEP 2 — VERIFY AFTER (every approved player should show mmr=200 now)
-- ============================================================
select count(*) as approved_players,
       count(*) filter (where mmr = 200) as players_at_200,
       count(*) filter (where mmr != 200) as players_NOT_at_200  -- should be 0
from players
where status = 'approved';

-- peak_mmr / peak_rank confirmed UNTOUCHED (spot check a few known
-- Season 1 top players — replace with real igns before running for
-- real, this is just a template).
-- select ign, mmr, peak_mmr, current_rank, peak_rank from players
-- where ign in ('GodLSkullG', 'SumitCantSnipe', 'Master.Fps');
