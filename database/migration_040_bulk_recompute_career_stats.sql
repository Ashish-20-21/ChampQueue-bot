-- migration_040_bulk_recompute_career_stats.sql
-- Branch: 8-db-burst-calls-reduction-and-wrapping-the-extra-calls
--
-- Problem: every match submission/approval fired recompute_player_career_stats
-- once per player -- 10 RPC requests in the same millisecond over ONE shared
-- HTTP/2 connection. In the Sep 21-29 logs every big "Server disconnected" /
-- KeyError cluster (6-20 failures at once) lined up with one of these bursts.
--
-- Fix: one RPC that takes all the player ids and runs the EXISTING
-- recompute_player_career_stats() for each of them inside the database.
-- The per-player maths is not touched or copied -- this only calls it.
--
-- House rule (engineering-rules): per-player bulk work uses a FOR ... LOOP with
-- PERFORM. The bare `select func(id) from players` pattern silently drops rows
-- on prod. Each player runs in its own sub-transaction (BEGIN/EXCEPTION) so
-- one bad player cannot roll back the other nine; the ids that failed are
-- RETURNED so the bot can report exactly who is stale. Empty array = all ok.
--
-- Ids are de-duplicated and processed in ascending order so two overlapping
-- calls always take row locks in the same order.
--
-- NOT touched: season points (season_points / season_point_events and the
-- per-step "no negative SP" floor). recompute_player_career_stats does not
-- reference them, so neither does this.

create or replace function recompute_player_career_stats_bulk(p_player_ids bigint[])
returns bigint[]
language plpgsql
as $$
declare
    v_pid    bigint;
    v_failed bigint[] := '{}';
begin
    if p_player_ids is null then
        return v_failed;
    end if;

    for v_pid in
        select distinct u from unnest(p_player_ids) as u order by u
    loop
        begin
            perform recompute_player_career_stats(v_pid);
        exception when others then
            v_failed := v_failed || v_pid;
            raise warning 'recompute_player_career_stats_bulk: player % failed: % (%)',
                v_pid, sqlerrm, sqlstate;
        end;
    end loop;

    return v_failed;
end;
$$;

-- migration_030 lesson: new objects need an explicit service_role grant.
grant execute on function recompute_player_career_stats_bulk(bigint[]) to service_role;
