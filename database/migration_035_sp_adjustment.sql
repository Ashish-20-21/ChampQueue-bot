-- ============================================================
-- MIGRATION 035: Manual Season Points Adjustment
-- ------------------------------------------------------------
-- Depends on: migration_029 (season_points, season_point_events,
-- is_season_points_locked, lock_season_points).
--
-- Mirrors /admin-adjust-mmr's shape (admin.py's apply_mmr_adjustment
-- + mmr_adjustment_log) for a disciplinary/manual points version —
-- but SP is NOT a straight copy of that pattern, for one critical
-- reason: unlike players.mmr (a plain field with no recompute-from-
-- source mechanism), season_points.points IS routinely rebuilt from
-- scratch by recompute_player_season_points()/recompute_all_season_
-- points(), which sum(delta) purely from season_point_events. A
-- manual adjustment written ONLY to a separate audit table (the
-- naive mmr_adjustment_log-style copy) would silently vanish the
-- next time anyone runs the existing /admin-recompute-points repair
-- tool — the exact "silent data loss" landmine class this project
-- has hit before elsewhere. So a manual SP adjustment has to be a
-- real season_point_events row (match_id = null) to survive
-- recompute, in addition to a human-readable audit log for who/why
-- (which season_point_events itself has no columns for).
--
-- Adds:
--   1. season_point_events.match_id made nullable — manual
--      adjustments have no match to attach to.
--   2. sp_adjustment_log — audit trail (mirrors mmr_adjustment_log).
--   3. apply_sp_adjustment() — writes both the event (recompute-safe)
--      and the audit log row, then updates the cached season_points
--      total. Respects the same season-lock guard as match-driven
--      points (update_season_points_for_match) and the same
--      floor-at-0 rule.
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- 1. Allow a null match_id — manual adjustments aren't tied to
--    a specific match. NULLs are distinct from each other under
--    Postgres UNIQUE semantics, so this does not weaken the
--    existing unique(season_id, player_id, match_id) constraint
--    for real match-driven rows; multiple manual adjustments for
--    the same player/season simply won't collide with it.
-- ────────────────────────────────────────────────────────────
alter table season_point_events alter column match_id drop not null;


-- ────────────────────────────────────────────────────────────
-- 2. sp_adjustment_log — human-readable audit trail (who/why),
--    same shape as mmr_adjustment_log. season_point_events has
--    no reason/adjusted_by columns and isn't meant to grow them
--    (it's a per-match delta ledger) — this is the SP equivalent
--    of what mmr_adjustment_log already does for MMR.
-- ────────────────────────────────────────────────────────────
create table if not exists sp_adjustment_log (
    id              bigserial primary key,
    season_id       bigint not null references seasons(id),
    player_id       bigint not null references players(id),
    delta           integer not null,
    reason          text not null,
    adjusted_by     text not null,      -- admin discord_id, same convention as mmr_adjustment_log
    created_at      timestamptz not null default now()
);

create index if not exists idx_sp_adjustment_log_player on sp_adjustment_log(player_id, season_id);


-- ────────────────────────────────────────────────────────────
-- 3. apply_sp_adjustment() — the actual write path.
--    Guards:
--      - refuses if season points are already locked (season
--        ended, prize positions final) — same guard
--        update_season_points_for_match already applies to
--        match-driven point changes, for consistency.
--      - floor at 0 (never negative), same rule as every other
--        SP write path.
--    Writes, in order:
--      1. sp_adjustment_log row (audit — who, why, how much)
--      2. season_point_events row, match_id = null (so this
--         survives any future recompute_player_season_points /
--         recompute_all_season_points call)
--      3. season_points upsert (the cached total everything else
--         actually reads)
--    Then re-runs the same season-end threshold check
--    update_season_points_for_match does, in case a positive
--    manual adjustment happens to push someone over the lock
--    threshold (rare for a "punishment" use case, but a positive
--    correction is equally possible and should behave identically
--    to a match-driven one crossing the line).
-- ────────────────────────────────────────────────────────────
create or replace function apply_sp_adjustment(
    p_player_id bigint,
    p_season_id bigint,
    p_delta integer,
    p_reason text,
    p_adjusted_by text
)
returns table (
    player_id bigint,
    season_id bigint,
    points integer
)
language plpgsql
as $$
declare
    v_already_locked boolean;
    v_threshold constant integer := 2500;
    v_first_player_id bigint;
begin
    select is_season_points_locked(p_season_id) into v_already_locked;
    if v_already_locked then
        raise exception 'Season points are locked for season_id=% — cannot adjust', p_season_id;
    end if;

    -- 1. Audit log — always written, regardless of what happens below,
    --    so there's a record even if this is later investigated.
    insert into sp_adjustment_log (season_id, player_id, delta, reason, adjusted_by)
    values (p_season_id, p_player_id, p_delta, p_reason, p_adjusted_by);

    -- 2. Event row — match_id null marks this as a manual adjustment,
    --    not a match result. This is what makes the adjustment
    --    recompute-safe (see migration header).
    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    values (p_season_id, p_player_id, null, p_delta, false);

    -- 3. Cached total — same additive upsert pattern as
    --    update_season_points_for_match, floor at 0.
    insert into season_points (season_id, player_id, points, updated_at)
    values (p_season_id, p_player_id, greatest(0, p_delta), now())
    on conflict (season_id, player_id) do update
        set points = greatest(0, season_points.points + p_delta),
            updated_at = now();

    -- Season-end check, same as update_season_points_for_match.
    select sp.player_id into v_first_player_id
    from season_points sp
    where sp.season_id = p_season_id
      and sp.points >= v_threshold
      and sp.is_locked = false
    order by sp.points desc, sp.updated_at asc
    limit 1;

    if v_first_player_id is not null then
        perform lock_season_points(p_season_id, v_first_player_id);
    end if;

    return query
        select sp.player_id, sp.season_id, sp.points
        from season_points sp
        where sp.season_id = p_season_id and sp.player_id = p_player_id;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 4. get_sp_adjustment_log() — lookup for an admin command that
--    wants to show a player's adjustment history, same as
--    get_mmr_adjustment_log's Python-side equivalent (that one's a
--    plain table select, not an RPC — this mirrors it as a simple
--    RPC for consistency with the rest of this migration, but a
--    plain .select() from Python works identically and needs no
--    RPC at all if preferred).
-- ────────────────────────────────────────────────────────────
create or replace function get_sp_adjustment_log(
    p_player_id bigint,
    p_season_id bigint default null,
    p_limit integer default 10
)
returns setof sp_adjustment_log
language sql
as $$
    select *
    from sp_adjustment_log
    where player_id = p_player_id
      and (p_season_id is null or season_id = p_season_id)
    order by created_at desc
    limit p_limit;
$$;


-- Sanity checks (run manually after applying, adjust IDs to real
-- values from your own DB before running):
-- select apply_sp_adjustment(121, 2, -10, 'test: AFK penalty', '111111111111111111');
-- select * from sp_adjustment_log where player_id = 121 order by created_at desc limit 5;
-- select * from season_point_events where player_id = 121 and match_id is null;
-- select * from season_points where player_id = 121 and season_id = 2;
