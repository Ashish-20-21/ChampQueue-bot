-- ============================================================
-- MIGRATION 010: Unified global region, 4-queue matchmaking,
-- admin-uploads-scoreboard exception, unified leaderboard
-- ------------------------------------------------------------
-- Run after migration_009_drop_map_votes.sql. Decisions locked with
-- Noiceee_pro 2026-07-29 (see DECISIONS.md for the full writeup):
--   - players.region becomes informational-only, never a matchmaking
--     gate. New queue_key column on queue_entries/matches is the real
--     source of truth for "which of the 4 queues".
--   - Existing East/West rows are left untouched — not migrated,
--     not backfilled to a new region. Constraints are WIDENED, not
--     replaced, so old data stays valid and new signups get the new
--     4-region set.
--   - New region values: EU_AF, NA_LATAM, INDIA_ME, JAPAN
--   - New queue_key values: same 4 strings (region and queue_key
--     happen to share a vocabulary today since there's one queue per
--     region at launch, but they are DIFFERENT COLUMNS with different
--     meanings — region is a player-level label set once at
--     registration, queue_key is a per-queue-entry/per-match fact
--     about which physical queue a match formed in. Do not collapse
--     them back into one column; that's the exact coupling this
--     migration removes.)
-- ============================================================

-- ------------------------------------------------------------
-- 1. players.region: widen constraint, do not touch existing rows.
-- ------------------------------------------------------------
alter table players drop constraint if exists players_region_check;
alter table players add constraint players_region_check check (
    region in ('East', 'West', 'EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN')
);

-- ------------------------------------------------------------
-- 2. queue_entries: add queue_key. This is the NEW source of truth
--    for which queue a waiting player is in — decoupled from
--    players.region. Nullable initially so this migration is safe
--    to run against live 'waiting' rows (they'll be re-created by
--    normal queue churn within minutes); backfill is not attempted
--    since a stale queue_entries row is transient by nature (it's
--    either matched out or left within the session).
-- ------------------------------------------------------------
alter table queue_entries add column if not exists queue_key text;
alter table queue_entries drop constraint if exists queue_entries_queue_key_check;
alter table queue_entries add constraint queue_entries_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN')
);
create index if not exists idx_queue_entries_queue_key on queue_entries(queue_key);

-- ------------------------------------------------------------
-- 3. matches: add queue_key alongside the existing region column.
--    region is LEFT IN PLACE (do not drop — old rows still have
--    'East'/'West' and downstream tooling/queries may still read
--    it for historical reference). queue_key is what new code reads
--    for match-time queue provenance; it is NOT used to gate upload/
--    approval/leaderboard channels anymore — those are unified.
--    Backfill existing matches' queue_key from their own region
--    value where it happens to already be a valid queue_key (it
--    won't be, since old rows are East/West) — so this backfill is
--    a no-op today and only documents the intent for future runs.
-- ------------------------------------------------------------
alter table matches add column if not exists queue_key text;
alter table matches drop constraint if exists matches_queue_key_check;
alter table matches add constraint matches_queue_key_check check (
    queue_key is null or queue_key in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN')
);

update matches
set queue_key = region
where queue_key is null
  and region in ('EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN');

-- Widen matches_region_check the same way as players_region_check —
-- existing East/West matches stay valid, new matches can carry any
-- of the 4 new values too (in case region ever gets read again for
-- reporting; harmless to keep in sync with players).
alter table matches drop constraint if exists matches_region_check;
alter table matches add constraint matches_region_check check (
    region in ('East', 'West', 'EU_AF', 'NA_LATAM', 'INDIA_ME', 'JAPAN')
);

-- ------------------------------------------------------------
-- 4. match_screenshots.uploaded_by: relax the FK to players(id).
--    Reason: admins uploading on a host's behalf (new exception,
--    DECISIONS.md 2026-07-29) may not have a players row at all —
--    an admin is a Discord role, not necessarily a registered
--    competitive player. Storing the raw Discord user ID instead
--    of a players.id FK keeps this column meaningful for both
--    cases (host upload: player's Discord ID; admin upload: admin's
--    Discord ID) without a nullable-FK special case.
--    NOTE: this changes the *meaning* of uploaded_by from "players.id"
--    to "discord user id (as bigint)". Existing rows were already
--    populated with player["id"] values (see database/db.py's
--    _upsert_match_screenshot pre-migration) — this migration does
--    NOT rewrite historical rows' values, only the constraint. Old
--    rows remain valid integers, just no longer FK-checked. If you
--    need to distinguish old-style (players.id) rows from new-style
--    (discord_id) rows later, use created_at < this migration's
--    run date as the cutoff.
-- ------------------------------------------------------------
alter table match_screenshots drop constraint if exists match_screenshots_uploaded_by_fkey;
-- uploaded_by stays NOT NULL and bigint — only the FK is dropped.

-- ------------------------------------------------------------
-- 5. Unified leaderboard RPCs — drop the p_region filter entirely.
--    Both functions keep their original names/signatures changed to
--    zero-arg so existing RPC call sites (db.py) only need their
--    Python call updated to stop passing a region, no renaming.
-- ------------------------------------------------------------
drop function if exists public.region_leaderboard(text);

create or replace function public.region_leaderboard()
 returns table(id bigint, ign text, mmr integer, peak_mmr integer, current_rank text, wins integer, losses integer, mvp_count integer)
 language sql
 stable
as $function$
    -- current_rank is DERIVED from mmr here, not read from
    -- players.current_rank — see the big comment above approve_ro3_match
    -- for the full "why" (players.current_rank is write-only-at-approval,
    -- can go stale relative to the actual mmr, confirmed live 2026-07-19).
    --
    -- Unified 2026-07-29: dropped "where region = p_region" — leaderboard
    -- is now global across all 4 queues/regions combined, per the
    -- unified-region decision. Function name kept as region_leaderboard
    -- (not renamed) so this is a pure body swap, no call-site renaming
    -- needed beyond dropping the now-unused argument.
    select id, ign, mmr, peak_mmr,
        case
            when greatest(0, mmr) >= 1501 then 'Titans'
            when greatest(0, mmr) >= 1351 then 'Legendary2'
            when greatest(0, mmr) >= 1201 then 'Legendary1'
            when greatest(0, mmr) >= 1051 then 'Grandmaster2'
            when greatest(0, mmr) >= 901  then 'Grandmaster1'
            when greatest(0, mmr) >= 751  then 'Master2'
            when greatest(0, mmr) >= 601  then 'Master1'
            when greatest(0, mmr) >= 451  then 'PRO2'
            when greatest(0, mmr) >= 301  then 'PRO1'
            when greatest(0, mmr) >= 151  then 'Elite2'
            else 'Elite1'
        end as current_rank,
        wins, losses, mvp_count
    from players
    where status = 'approved'
    order by mmr desc, id asc;
$function$;

drop function if exists public.weekly_leaders(text);

create or replace function public.weekly_leaders()
 returns table(category text, player_id bigint, value numeric)
 language sql
 stable
as $function$
    -- Unified 2026-07-29: dropped "and p.region = p_region" — weekly
    -- badges are now awarded globally across all 4 queues/regions,
    -- not per-region. Same reasoning as region_leaderboard above.
    with week_rounds as (
        select mps.*, mrr.is_mvp, mrr.team
        from match_player_stats mps
        join match_round_results mrr
            on mrr.match_id = mps.match_id and mrr.round_number = mps.round_number and mrr.player_id = mps.player_id
        join matches m on m.id = mps.match_id
        join players p on p.id = mps.player_id
        where m.status = 'completed'
          and m.completed_at >= now() - interval '7 days'
    ),
    per_player as (
        select player_id,
               sum(kills) as total_kills,
               sum(hill_time) as total_hill,
               sum(impact) filter (where impact is not null) as total_impact,
               count(*) filter (where is_mvp) as mvp_count,
               count(distinct match_id) as matches_played
        from week_rounds
        group by player_id
    ),
    top_mvp as (
        select 'most_mvp' as category, player_id, mvp_count::numeric as value
        from per_player order by mvp_count desc, player_id asc limit 1
    ), top_kills as (
        select 'top_kills' as category, player_id, total_kills::numeric as value
        from per_player order by total_kills desc, player_id asc limit 1
    ), top_obj as (
        select 'top_obj' as category, player_id, total_hill::numeric as value
        from per_player order by total_hill desc, player_id asc limit 1
    ), top_impact as (
        select 'top_impact' as category, player_id, total_impact::numeric as value
        from per_player where total_impact is not null order by total_impact desc, player_id asc limit 1
    ), top_matches as (
        select 'most_matches' as category, player_id, matches_played::numeric as value
        from per_player order by matches_played desc, player_id asc limit 1
    )
    select category, player_id, value from top_mvp
    union all
    select category, player_id, value from top_kills
    union all
    select category, player_id, value from top_obj
    union all
    select category, player_id, value from top_impact
    union all
    select category, player_id, value from top_matches;
$function$;

-- ------------------------------------------------------------
-- Sanity checks — run after applying, expect the results noted.
-- ------------------------------------------------------------
-- select region_leaderboard() limit 5;                 -- should return top 5 globally, no error
-- select * from weekly_leaders();                       -- should return up to 5 rows, no error
-- select conname from pg_constraint where conname = 'match_screenshots_uploaded_by_fkey';  -- should return 0 rows
-- select column_name from information_schema.columns where table_name = 'queue_entries' and column_name = 'queue_key';  -- should return 1 row
-- select column_name from information_schema.columns where table_name = 'matches' and column_name = 'queue_key';  -- should return 1 row
