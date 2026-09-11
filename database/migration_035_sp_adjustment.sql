-- ============================================================
-- MIGRATION 035: Manual Season Points Adjustment + Threshold/
--                 Payout Correction
-- ------------------------------------------------------------
-- AMENDED 2026-09-11: originally shipped as just the
-- /admin-adjust-sp feature (sections 1-4 below); a second,
-- unrelated fix (threshold 2500->3500 and payout amounts) was
-- drafted separately as migration_036 and has been folded in here
-- as section 6, to keep one file instead of two for this general
-- area. That second fix has NOTHING to do with /admin-adjust-sp —
-- it corrects a pre-existing bug in migration_029's match-driven
-- code (update_season_points_for_match, lock_season_points,
-- recompute_season_points_for_match) that predates this feature
-- entirely. Noted here so anyone reading migration history later
-- isn't confused about why one file covers two unrelated concerns.
--
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
--   4. get_sp_adjustment_log() — lookup for a player's adjustment
--      history.
--   5. (Unrelated fix, folded in — see AMENDED note above) Corrects
--      migration_029's hardcoded season-end threshold (2500 -> 3500)
--      and prize payouts (₹500/300/200 -> ₹700/500/300) to match
--      config.py's already-documented PRIZE_1ST/PRIZE_2ND_CAP/
--      PRIZE_3RD_CAP/SEASON_END_THRESHOLD values, which the SQL side
--      never actually read — same "duplicated across N copies"
--      landmine class as the rank-tier table. (This is section 6
--      in the body below — service_role grants are section 5.)
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
-- Postgres refuses CREATE OR REPLACE when the OUT parameter row type
-- changes (confirmed live 2026-09-11: 42P13 "cannot change return
-- type of existing function" — a different set of OUT parameter
-- names counts as a different row type, even with the same input
-- signature). If this migration is being re-run after the
-- out_player_id/out_season_id/out_points rename, or the function
-- already exists from an earlier version with plain
-- player_id/season_id/points output columns, drop it first — this is
-- safe: it only removes the function definition, nothing in
-- sp_adjustment_log/season_point_events/season_points is touched.
drop function if exists apply_sp_adjustment(bigint, bigint, integer, text, text);

create or replace function apply_sp_adjustment(
    p_player_id bigint,
    p_season_id bigint,
    p_delta integer,
    p_reason text,
    p_adjusted_by text
)
returns table (
    out_player_id bigint,
    out_season_id bigint,
    out_points integer
)
language plpgsql
as $$
declare
    v_already_locked boolean;
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

    -- Deliberately NO season-end/auto-lock check here — REMOVED
    -- 2026-09-11 after a real incident. update_season_points_for_match
    -- (the match-driven path) has a similar "is anyone >=2500 and
    -- unlocked" check, and it's reasonably safe there because it only
    -- runs right after a real match result is written, so "someone
    -- just crossed 2500" is usually actually true of that match.
    -- Copying the identical check into a disciplinary/correction tool
    -- was a mistake: it doesn't check whether THIS adjustment caused
    -- anyone to cross the threshold, only whether ANYONE currently
    -- sits >=2500 and unlocked — so a completely unrelated +2
    -- correction to one player locked the entire season because a
    -- different player already happened to be sitting at exactly
    -- 2500 from earlier. Confirmed live 2026-09-11: /admin-adjust-sp
    -- on an unrelated player triggered a full SEASON LOCKED state,
    -- assigning payouts, with zero connection to the adjustment that
    -- triggered it. An admin correction/penalty should never have the
    -- power to end a season as a surprise side effect — season-end
    -- should only ever come from a real match crossing the threshold,
    -- or an explicit admin action, never implicitly from this command.

    -- Reminder: the column names below (sp.player_id, sp.season_id,
    -- sp.points) map POSITIONALLY into the out_player_id/out_season_id
    -- /out_points columns declared in RETURNS TABLE above — RETURN
    -- QUERY does not use column names or aliases from this SELECT to
    -- name the output, only position. So the actual JSON keys
    -- Supabase returns to the caller are out_player_id/out_season_id
    -- /out_points, NOT player_id/season_id/points — db.py and
    -- admin.py were updated to match (see apply_sp_adjustment's
    -- Python wrapper).
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


-- ────────────────────────────────────────────────────────────
-- 5. Grant service_role access to the new table.
--    Same class of miss as migration_030 already had to fix once
--    for season_points/point_shields/season_point_events — Supabase's
--    service_role doesn't automatically get table access on CREATE
--    TABLE, and this project has hit that gap three times now (018,
--    029->030, and this one). Caught live 2026-09-11 testing on
--    ebsleroxzikxxvqblzry: "permission denied for table
--    sp_adjustment_log" (42501) from apply_sp_adjustment(). Functions
--    with default (invoker) rights run as whatever role calls them,
--    so no separate GRANT EXECUTE needed — this is purely the
--    underlying table grant, same as migration_030's own finding.
-- ────────────────────────────────────────────────────────────
grant select, insert, update, delete on public.sp_adjustment_log to service_role;
grant usage, select on sequence public.sp_adjustment_log_id_seq to service_role;


-- ────────────────────────────────────────────────────────────
-- 6. Correct migration_029's season-end threshold and prize
--    payouts (originally drafted separately as migration_036,
--    folded in here 2026-09-11 — see AMENDED note at the top of
--    this file). NOT related to /admin-adjust-sp at all — this
--    fixes a pre-existing bug in the match-driven code path.
--
--    migration_029 hardcoded a season-end threshold of 2500 SP and
--    flat/capped payouts of ₹500 / ₹300 / ₹200 across three
--    separate SQL functions — but config.py has always documented
--    the REAL intended values in a comment right above the
--    constants: "Prize pool ₹1500 — 1st is fixed, 2nd/3rd are
--    min(points÷POINTS_TO_RUPEE, cap)." PRIZE_1ST = 700,
--    PRIZE_2ND_CAP = 500, PRIZE_3RD_CAP = 300, POINTS_TO_RUPEE = 5,
--    SEASON_END_THRESHOLD = 3500. These were never wired into the
--    SQL side — same "duplicated across N copies" landmine class
--    already documented for the rank-tier table.
--
--    Real-money consequence, confirmed by cross-referencing
--    points.py's _season_end_embed (which reads config.PRIZE_1ST
--    directly for the announcement TEXT): the season-end
--    announcement has always said "1st place: ₹700" while
--    lock_season_points() actually wrote payout_rupees=500 to the
--    database — the promised and recorded amounts never matched.
--
--    Confirmed live 2026-09-11: manually converting the admin's
--    requested SP ceilings (2nd=2500 SP, 3rd=1500 SP) via the
--    existing 5-points-per-rupee ratio lands EXACTLY on config.py's
--    documented ₹500 / ₹300 caps — strong independent confirmation
--    these are the real intended numbers, not a new decision made
--    here.
--
--    Uses CREATE OR REPLACE (same signatures/shapes as
--    migration_029 — no DROP FUNCTION needed, unlike
--    apply_sp_adjustment's OUT-parameter rename above). Full
--    function bodies below are carried over unchanged from
--    migration_029 apart from the specific constants noted inline
--    — verified line-by-line against the migration_029 originals
--    before merging (only comments were trimmed; no logic dropped).
-- ────────────────────────────────────────────────────────────

create or replace function update_season_points_for_match(
    p_match_id bigint,
    p_season_id bigint
)
returns void
language plpgsql
as $$
declare
    v_threshold constant integer := 3500;  -- was 2500 — see section 6 header
    v_already_locked boolean;
    v_first_player_id bigint;
    v_first_points integer;
begin
    select is_season_points_locked(p_season_id) into v_already_locked;
    if v_already_locked then
        return;
    end if;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        p_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0
                then 5
            else
                case
                    when has_active_shield(mrr.player_id, p_season_id)
                        then 0
                    else -3
                end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0
                 and has_active_shield(mrr.player_id, p_season_id)
                then true
            else false
        end
    from match_round_results mrr
    where mrr.match_id = p_match_id
    on conflict (season_id, player_id, match_id) do update
        set delta = excluded.delta,
            was_shielded = excluded.was_shielded;

    insert into season_points (season_id, player_id, points, updated_at)
    select
        p_season_id,
        spe.player_id,
        greatest(0, spe.delta),
        now()
    from season_point_events spe
    where spe.match_id = p_match_id and spe.season_id = p_season_id
    on conflict (season_id, player_id) do update
        set points = greatest(0, season_points.points + (
                select spe2.delta
                from season_point_events spe2
                where spe2.match_id = p_match_id
                  and spe2.season_id = p_season_id
                  and spe2.player_id = season_points.player_id
            )),
            updated_at = now();

    select sp.player_id, sp.points
    into v_first_player_id, v_first_points
    from season_points sp
    where sp.season_id = p_season_id
      and sp.points >= v_threshold
      and sp.is_locked = false
    order by sp.points desc, sp.updated_at asc
    limit 1;

    if v_first_player_id is not null then
        perform lock_season_points(p_season_id, v_first_player_id);
    end if;
end;
$$;


create or replace function lock_season_points(
    p_season_id bigint,
    p_winner_player_id bigint
)
returns void
language plpgsql
as $$
declare
    v_points_to_rupee constant integer := 5;
    v_prize_1st constant integer := 700;   -- was 500 — see section 6 header
    v_cap_2nd constant integer := 500;     -- was 300 (= 2500 SP equivalent now, was 1500)
    v_cap_3rd constant integer := 300;     -- was 200 (= 1500 SP equivalent now, was 1000)
begin
    -- Step 1: Lock ALL rows (everyone's points freeze)
    update season_points
    set is_locked = true,
        locked_at = now()
    where season_id = p_season_id;

    -- Step 2: Assign #1 — the player who crossed the threshold
    update season_points
    set locked_rank = 1,
        payout_rupees = v_prize_1st
    where season_id = p_season_id
      and player_id = p_winner_player_id;

    -- Step 3: Assign #2 — highest points excluding #1
    update season_points
    set locked_rank = 2,
        payout_rupees = least(points / v_points_to_rupee, v_cap_2nd)
    where season_id = p_season_id
      and player_id = (
          select sp2.player_id
          from season_points sp2
          where sp2.season_id = p_season_id
            and sp2.player_id != p_winner_player_id
          order by sp2.points desc, sp2.updated_at asc
          limit 1
      );

    -- Step 4: Assign #3 — highest points excluding #1 and #2
    update season_points
    set locked_rank = 3,
        payout_rupees = least(points / v_points_to_rupee, v_cap_3rd)
    where season_id = p_season_id
      and player_id = (
          select sp3.player_id
          from season_points sp3
          where sp3.season_id = p_season_id
            and sp3.locked_rank is null
            and sp3.player_id != p_winner_player_id
          order by sp3.points desc, sp3.updated_at asc
          limit 1
      );
end;
$$;


create or replace function recompute_season_points_for_match(p_match_id bigint)
returns void
language plpgsql
as $$
declare
    v_season_id bigint;
    v_locked boolean;
begin
    select season_id into v_season_id
    from matches
    where id = p_match_id;

    if v_season_id is null then
        return;
    end if;

    select is_season_points_locked(v_season_id) into v_locked;

    delete from season_point_events
    where match_id = p_match_id and season_id = v_season_id;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        v_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0
                then 5
            else
                case
                    when exists (
                        select 1 from point_shields ps
                        where ps.player_id = mrr.player_id
                          and ps.season_id = v_season_id
                          and ps.status in ('active', 'expired')
                          and ps.shield_starts_at <= m.completed_at
                          and ps.shield_ends_at > m.completed_at
                    ) then 0
                    else -3
                end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0
                 and exists (
                    select 1 from point_shields ps
                    where ps.player_id = mrr.player_id
                      and ps.season_id = v_season_id
                      and ps.status in ('active', 'expired')
                      and ps.shield_starts_at <= m.completed_at
                      and ps.shield_ends_at > m.completed_at
                 ) then true
            else false
        end
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    where mrr.match_id = p_match_id;

    perform recompute_player_season_points(spe.player_id, v_season_id)
    from (select distinct player_id from season_point_events where match_id = p_match_id and season_id = v_season_id) spe;

    if v_locked then
        if not exists (
            select 1 from season_points
            where season_id = v_season_id
              and locked_rank = 1
              and points >= 3500  -- was 2500 — see section 6 header
        ) then
            update season_points
            set is_locked = false,
                locked_at = null,
                locked_rank = null,
                payout_rupees = null
            where season_id = v_season_id;
        end if;
    end if;
end;
$$;


-- Sanity checks (run manually after applying, adjust IDs to real
-- values from your own DB before running):
-- select apply_sp_adjustment(121, 2, -10, 'test: AFK penalty', '111111111111111111');
-- select * from sp_adjustment_log where player_id = 121 order by created_at desc limit 5;
-- select * from season_point_events where player_id = 121 and match_id is null;
-- select * from season_points where player_id = 121 and season_id = 2;