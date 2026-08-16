-- migration_017_atomic_round_data_write.sql
--
-- Root-cause fix for the write-race documented in
-- incident_CQ-8758_2026-08-12.txt (Root Cause #1, "NOT YET FIXED" at
-- the time that note was written).
--
-- Original problem: cogs/match.py's _submit_body wrote
-- match_round_results and match_player_stats via two separate Python
-- functions (_replace_match_round_results, _replace_match_player_stats),
-- each doing DELETE-then-INSERT as two non-atomic Supabase/PostgREST
-- HTTP calls. Under RO3 (3 rounds/match) these ran concurrently via
-- asyncio.gather across rounds, and a transient network blip mid-write
-- on one round could interleave with another round's in-flight write,
-- producing a genuine partial write (confirmed live in the CQ-8758
-- incident log, 2026-08-12 14:43:23-24 UTC — one round's insert count
-- came out short).
--
-- RO1 (migration_015) removed the cross-round concurrency that made
-- CQ-8758's specific interleaving possible -- _submit_body now only
-- ever processes one round per submission. But the underlying
-- non-atomicity was never actually fixed, only reduced in blast
-- radius: a single round's delete+insert was, and until this
-- migration still is, two separate non-atomic calls per table, and
-- ALSO two entirely separate write operations (round_results,
-- player_stats) that weren't atomic *with each other* either -- a
-- blip between the two could leave one table reflecting the round and
-- the other not.
--
-- This migration collapses both tables' delete+insert into a single
-- Postgres function call, run inside one implicit transaction. Either
-- the whole round (both tables) lands, or none of it does -- no
-- partial state possible, same guarantee approve_match() already
-- gives for MMR commits. This directly implements the "RECOMMENDED
-- FIX" section of the CQ-8758 incident note, and additionally covers
-- the round_results/player_stats cross-table gap that note didn't
-- separately call out.
--
-- Combined into ONE rpc call (not two) rather than one-per-table:
-- cogs/match.py's _submit_body always calls both for the same round
-- back-to-back, so splitting them would still leave a cross-table
-- atomicity gap even after each individually became safe. See
-- DECISIONS.md / session discussion 2026-08-16 for the plain-text
-- design discussion before this was written.
-- ============================================================

create or replace function replace_match_round_data(
    p_match_id bigint,
    p_round_number int,
    p_round_results jsonb,
    p_player_stats jsonb
) returns void
language plpgsql
as $$
begin
    -- match_round_results: same delete-then-insert shape as the old
    -- Python _replace_match_round_results, just inside one transaction
    -- instead of two separate HTTP round trips.
    delete from match_round_results
    where match_id = p_match_id and round_number = p_round_number;

    if p_round_results is not null and jsonb_array_length(p_round_results) > 0 then
        insert into match_round_results (match_id, round_number, player_id, position, is_mvp, mmr_delta, team)
        select p_match_id, p_round_number,
               (row->>'player_id')::bigint,
               (row->>'position')::integer,
               coalesce((row->>'is_mvp')::boolean, false),
               (row->>'mmr_delta')::integer,
               row->>'team'
        from jsonb_array_elements(p_round_results) as row;
    end if;

    -- match_player_stats: same shape, same transaction. Failing partway
    -- through either table's write now rolls back BOTH tables for this
    -- round, not just the one call in flight.
    delete from match_player_stats
    where match_id = p_match_id and round_number = p_round_number;

    if p_player_stats is not null and jsonb_array_length(p_player_stats) > 0 then
        insert into match_player_stats (match_id, round_number, player_id, kills, deaths, assists, damage, hill_time, impact, score)
        select p_match_id, p_round_number,
               (row->>'player_id')::bigint,
               (row->>'kills')::integer,
               (row->>'deaths')::integer,
               (row->>'assists')::integer,
               nullif(row->>'damage', '')::integer,
               (row->>'hill_time')::numeric(6,2),
               nullif(row->>'impact', '')::numeric(6,2),
               (row->>'score')::integer
        from jsonb_array_elements(p_player_stats) as row;
    end if;
end;
$$;

-- Old Python-level functions (_replace_match_round_results,
-- _replace_match_player_stats in database/db.py) are superseded by
-- this RPC and should be removed from db.py in the same commit that
-- updates cogs/match.py's _submit_body to call replace_match_round_data
-- instead. Not dropped here at the SQL level since nothing at the SQL
-- level defined them -- they were pure Python/PostgREST calls against
-- the tables directly, no corresponding function to drop.
