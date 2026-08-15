-- migration_016_fix_stale_rank_defaults.sql
--
-- Root-cause fix for the 'Elite'/'II' rows found live 2026-08-15
-- (screenshot: players.current_rank/current_division showing a mix of
-- old-scheme 'Elite'/'II' and new-scheme 'Elite1'/'Elite2'/'' side by
-- side — 85 rows on the old scheme, all status='approved', all sitting
-- at mmr=200/peak_mmr=200 (the post-reset default), never having had a
-- match approved. Confirmed via direct SQL, not guessed).
--
-- Root cause: schema.sql's original `create table players` column
-- defaults were never updated when migration_012 widened rank bands to
-- 200 points and moved to the flat Elite1/Elite2 (no division) scheme.
-- migration_012 bumped the MMR column defaults (mmr/peak_mmr: 1000 -> 200)
-- but did NOT bump current_rank/current_division/peak_rank's defaults —
-- those were only ever fixed via a one-time UPDATE against rows that
-- existed AT THAT MOMENT. database/db.py's create_player() never sets
-- these columns explicitly, so any approved player who has never had a
-- match go through approve_match() has been silently sitting on the
-- pre-esports-era defaults (current_rank='Elite', current_division='II',
-- peak_rank='Elite') since the day they registered — this is an ONGOING
-- gap for every not-yet-played player, not just leftover historical data.
--
-- SECOND finding, same session: current_division is dead weight. Every
-- live writer (approve_match in migration_015_ro1.sql, and this
-- migration's own backfill) sets it to '' unconditionally — it has never
-- carried a real value since the migration_012 rename away from the old
-- two-part Elite/I/II scheme. Repo-wide grep (2026-08-15) confirmed
-- exactly one live, non-dead reader: cogs/registration.py's /whoami
-- (fixed in this same commit to drop it from the display string).
-- utils/embeds.py's profile_card() and leaderboard_embed() also
-- referenced it but have ZERO callers (confirmed via grep) — updated
-- anyway to avoid leaving a landmine if either is ever wired up later.
-- Given it's provably dead everywhere, this migration also DROPS the
-- column outright rather than just backfilling it to '' forever.
--
-- This migration does three things, in order:
--   1. Fixes current_rank/peak_rank/mmr/peak_mmr column defaults so
--      future registrations get correct values immediately (closes the
--      rank/MMR half of the bug at the source).
--   2. Backfills every EXISTING row's current_rank/peak_rank to match
--      what mmr_engine.derive_rank() would compute for their current
--      mmr/peak_mmr, and normalizes current_division to '' as a clean
--      pre-drop checkpoint — supersedes the stale
--      database/backfill_current_rank.sql (which used the OLD 150-point
--      bands, pre-migration_012, and never touched current_division at
--      all — that's the file that stopped being correct and is why this
--      migration exists). Self-contained here, no need to run
--      backfill_current_rank.sql separately/first.
--   3. Drops current_division entirely (commented out below — run as a
--      deliberate separate step, not automatically).
--
-- Steps 1-2 are safe to run any number of times (fixed defaults are
-- idempotent; the backfill UPDATE is a plain derive-and-set, not
-- additive). Step 3 is a one-way schema change — run steps 1-2, confirm
-- with the sanity check below, THEN uncomment and run step 3 on its own.
--
-- Not required for anything the bot actually displays to players —
-- region_leaderboard(), player_stats_card(), and rank_progress_card() all
-- derive rank LIVE from mmr via mmr_engine.derive_rank(), never trust
-- players.current_rank directly (see utils/embeds.py). This fixes the RAW
-- table so direct Supabase queries / admin lookups / digest.py also read
-- correctly, and so new registrations don't keep re-introducing the stale
-- values going forward.

-- ============================================================
-- STEP 1 — fix column defaults for all future inserts
-- ============================================================
alter table players alter column mmr set default 200;
alter table players alter column peak_mmr set default 200;
alter table players alter column current_rank set default 'Elite1';
alter table players alter column peak_rank set default 'Elite1';
-- current_division's default is intentionally left untouched here — it's
-- dropped outright in step 3, so its default stops mattering.

-- ============================================================
-- STEP 2 — backfill every existing row
-- ============================================================
-- 200-point bands, matching mmr_engine.py's _TIER_LADDER and
-- migration_015_ro1.sql's approve_match() CASE chain exactly. Keep all
-- three in sync by hand on any future band change (see
-- mmr_engine.derive_rank's docstring).
update players
set current_rank = case
        when greatest(0, mmr) >= 2001 then 'Titans'
        when greatest(0, mmr) >= 1801 then 'Legendary2'
        when greatest(0, mmr) >= 1601 then 'Legendary1'
        when greatest(0, mmr) >= 1401 then 'Grandmaster2'
        when greatest(0, mmr) >= 1201 then 'Grandmaster1'
        when greatest(0, mmr) >= 1001 then 'Master2'
        when greatest(0, mmr) >= 801  then 'Master1'
        when greatest(0, mmr) >= 601  then 'PRO2'
        when greatest(0, mmr) >= 401  then 'PRO1'
        when greatest(0, mmr) >= 201  then 'Elite2'
        else 'Elite1'
    end,
    current_division = '',
    peak_rank = case
        when greatest(0, peak_mmr) >= 2001 then 'Titans'
        when greatest(0, peak_mmr) >= 1801 then 'Legendary2'
        when greatest(0, peak_mmr) >= 1601 then 'Legendary1'
        when greatest(0, peak_mmr) >= 1401 then 'Grandmaster2'
        when greatest(0, peak_mmr) >= 1201 then 'Grandmaster1'
        when greatest(0, peak_mmr) >= 1001 then 'Master2'
        when greatest(0, peak_mmr) >= 801  then 'Master1'
        when greatest(0, peak_mmr) >= 601  then 'PRO2'
        when greatest(0, peak_mmr) >= 401  then 'PRO1'
        when greatest(0, peak_mmr) >= 201  then 'Elite2'
        else 'Elite1'
    end
where current_rank <> case
        when greatest(0, mmr) >= 2001 then 'Titans'
        when greatest(0, mmr) >= 1801 then 'Legendary2'
        when greatest(0, mmr) >= 1601 then 'Legendary1'
        when greatest(0, mmr) >= 1401 then 'Grandmaster2'
        when greatest(0, mmr) >= 1201 then 'Grandmaster1'
        when greatest(0, mmr) >= 1001 then 'Master2'
        when greatest(0, mmr) >= 801  then 'Master1'
        when greatest(0, mmr) >= 601  then 'PRO2'
        when greatest(0, mmr) >= 401  then 'PRO1'
        when greatest(0, mmr) >= 201  then 'Elite2'
        else 'Elite1'
    end
   or current_division <> ''
   or peak_rank <> case
        when greatest(0, peak_mmr) >= 2001 then 'Titans'
        when greatest(0, peak_mmr) >= 1801 then 'Legendary2'
        when greatest(0, peak_mmr) >= 1601 then 'Legendary1'
        when greatest(0, peak_mmr) >= 1401 then 'Grandmaster2'
        when greatest(0, peak_mmr) >= 1201 then 'Grandmaster1'
        when greatest(0, peak_mmr) >= 1001 then 'Master2'
        when greatest(0, peak_mmr) >= 801  then 'Master1'
        when greatest(0, peak_mmr) >= 601  then 'PRO2'
        when greatest(0, peak_mmr) >= 401  then 'PRO1'
        when greatest(0, peak_mmr) >= 201  then 'Elite2'
        else 'Elite1'
    end;

-- Sanity check — run AFTER step 2, BEFORE step 3. Should return ZERO
-- rows. If it doesn't, STOP — do not run step 3 — and investigate.
--
-- select id, ign, mmr, peak_mmr, current_rank, current_division, peak_rank
-- from players
-- where current_division <> ''
--    or current_rank <> (
--        case
--            when greatest(0, mmr) >= 2001 then 'Titans'
--            when greatest(0, mmr) >= 1801 then 'Legendary2'
--            when greatest(0, mmr) >= 1601 then 'Legendary1'
--            when greatest(0, mmr) >= 1401 then 'Grandmaster2'
--            when greatest(0, mmr) >= 1201 then 'Grandmaster1'
--            when greatest(0, mmr) >= 1001 then 'Master2'
--            when greatest(0, mmr) >= 801  then 'Master1'
--            when greatest(0, mmr) >= 601  then 'PRO2'
--            when greatest(0, mmr) >= 401  then 'PRO1'
--            when greatest(0, mmr) >= 201  then 'Elite2'
--            else 'Elite1'
--        end
--    );

-- ============================================================
-- STEP 3 — drop current_division. Run ONLY after the sanity check above
-- returns zero rows. One-way, irreversible on prod without a manual
-- re-add + backfill. As of this commit, the only code that read this
-- column (cogs/registration.py /whoami, utils/embeds.py profile_card +
-- leaderboard_embed) has already been updated to not reference it —
-- confirmed via repo-wide grep before writing this migration.
-- Deliberately left commented out — uncomment and run as its own
-- statement, not bundled into a single blind paste of this whole file.
-- ============================================================
-- alter table players drop column current_division;

-- ============================================================
-- STEP 4 — re-point approve_match() to stop writing current_division.
-- MUST run in the same deployment as step 3, and MUST run AFTER step 3
-- (or at minimum before the next real match is approved) — the live
-- approve_match() function (see migration_015_ro1.sql) has
-- `current_division = ''` in its UPDATE ... SET list. Once the column
-- is dropped, the next call to approve_match() would fail outright with
-- an undefined-column error, blocking every match approval until this
-- runs. Identical to migration_015_ro1.sql's approve_match() in every
-- other respect — only the current_division line is removed. Not
-- editing migration_015_ro1.sql itself since it's an already-deployed
-- historical migration file; this follows the same pattern
-- migration_013 used to re-point region_leaderboard() after the fact.
-- ============================================================
-- create or replace function public.approve_match(p_match_id bigint, p_approved_by bigint)
--  returns table(player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
--  language plpgsql
-- as $function$
-- declare
--     v_match matches%rowtype;
-- begin
--     select * into v_match from matches where id = p_match_id for update;
--     if not found then
--         raise exception 'match % does not exist', p_match_id;
--     end if;
--     if v_match.status <> 'pending_verification' then
--         raise exception 'match % is not pending verification', p_match_id;
--     end if;
--     if (select count(*) from match_round_results where match_id = p_match_id) <> 10 then
--         raise exception 'match % must have exactly 10 round-result rows', p_match_id;
--     end if;
--
--     return query
--     with deltas as (
--         select mrr.player_id, sum(mrr.mmr_delta)::integer as total_delta
--         from match_round_results mrr
--         where mrr.match_id = p_match_id
--         group by mrr.player_id
--     ), updated_players as (
--         update players p
--         set mmr = greatest(0, p.mmr + d.total_delta),
--             peak_mmr = greatest(p.peak_mmr, greatest(0, p.mmr + d.total_delta)),
--             current_rank = case
--                 when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans'
--                 when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
--                 when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1'
--                 when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
--                 when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1'
--                 when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
--                 when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1'
--                 when greatest(0, p.mmr + d.total_delta) >= 601  then 'PRO2'
--                 when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1'
--                 when greatest(0, p.mmr + d.total_delta) >= 201  then 'Elite2'
--                 else 'Elite1'
--             end,
--             peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
--                 when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans' when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
--                 when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1' when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
--                 when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1' when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
--                 when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1' when greatest(0, p.mmr + d.total_delta) >= 601 then 'PRO2'
--                 when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1' when greatest(0, p.mmr + d.total_delta) >= 201 then 'Elite2' else 'Elite1' end
--                 else p.peak_rank end,
--             updated_at = now()
--         from deltas d
--         where p.id = d.player_id
--         returning p.id, p.mmr - d.total_delta as before_mmr, p.mmr as after_mmr, d.total_delta
--     ), updated_match_players as (
--         update match_players mp
--         set mmr_before = up.before_mmr, mmr_after = up.after_mmr, mmr_change = up.total_delta
--         from updated_players up
--         where mp.match_id = p_match_id and mp.player_id = up.id
--         returning up.id, up.before_mmr, up.after_mmr, up.total_delta
--     )
--     select * from updated_match_players;
--
--     update matches
--     set status = 'completed', completed_at = now(), approved_by = p_approved_by, approved_at = now()
--     where id = p_match_id;
-- end;
-- $function$;
--
-- Sanity check after step 4 — run a real /admin-force-approve or approve
-- flow on a test match, or directly:
--   select approve_match(<a real pending_verification match_id>, <admin_discord_id>);
-- and confirm it succeeds with no "column current_division does not
-- exist" error. approve_ro3_match stays a thin SQL alias delegating to
-- approve_match (unchanged, no edit needed — see migration_015_ro1.sql).

