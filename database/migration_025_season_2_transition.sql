-- migration_025_season_2_transition.sql
--
-- Season 1 -> Season 2 cutover. Written because get_active_season() in
-- db.py does `.eq("is_active", true)` with NO `.limit(1)` — flagged as a
-- known risk back in migration_023's header comment. If Season 2 were
-- inserted as active in a separate statement BEFORE Season 1 is
-- deactivated, there would be a window (however short) with two active
-- rows, and which one a match forming in that window gets tagged to
-- becomes non-deterministic. Everything here that touches is_active runs
-- in ONE transaction so that window can never exist.
--
-- Per house convention: verify before, verify after, never trust
-- "Success. No rows returned."

-- ============================================================
-- STEP 0 — VERIFY CURRENT STATE (run first, read the output)
-- ============================================================
select id, name, code, start_date, end_date, is_active
from seasons
order by id;
-- Expect exactly one row: Season 1 / S1-0826, is_active = true.
-- If you see anything else (already 2 rows, or none active), STOP —
-- that means the state assumption below is wrong and this migration
-- shouldn't be run as-is.

-- ============================================================
-- STEP 1 — ATOMIC CUTOVER
-- ============================================================
begin;

-- Deactivate every currently-active season (should be exactly Season 1,
-- verified in Step 0). Not narrowed to `where code = 'S1-0826'` on
-- purpose — if Step 0 ever showed more than one active row for any
-- reason, this still leaves the DB in a correct single-active-season
-- state rather than compounding the problem.
update seasons
set is_active = false
where is_active = true;

-- Insert Season 2. code is a free label like S1-0826 was — adjust the
-- date portion if this doesn't run same-day as the actual cutover.
insert into seasons (name, code, is_active, start_date)
values ('Season 2', 'S2-0901', true, now());

commit;

-- ============================================================
-- STEP 2 — VERIFY AFTER (must show exactly one active row: Season 2)
-- ============================================================
select id, name, code, start_date, end_date, is_active
from seasons
order by id;

-- Belt-and-suspenders: confirm no more than one active row exists, full
-- stop, independent of eyeballing Step 2's output.
select count(*) as active_season_count
from seasons
where is_active = true;
-- MUST return exactly 1. If it returns anything else, do not deploy
-- queue.py or run any matches until this is fixed by hand.

-- ============================================================
-- STEP 3 — CLOSE THE get_active_season() GAP (do this now, not later)
-- ============================================================
-- Even with Step 1 guaranteeing at most one active row going forward,
-- get_active_season() in db.py still has no LIMIT 1/ordering — belt
-- and suspenders at the DB layer costs nothing and protects against any
-- future manual edit that accidentally leaves two rows active. A
-- partial unique index enforces "at most one active season" at the
-- constraint level, so a bad UPDATE/INSERT fails loudly instead of
-- silently creating the exact non-determinism this migration exists to
-- avoid.
create unique index if not exists idx_seasons_one_active
    on seasons (is_active)
    where is_active = true;
-- With this index in place, Step 1's UPDATE-then-INSERT pattern above
-- is the ONLY safe way to do a cutover from now on — trying to INSERT a
-- new active row before deactivating the old one will now fail outright
-- (constraint violation) instead of silently succeeding into a bad
-- state. That's the intended effect.
