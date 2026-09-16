-- ============================================================
-- MIGRATION 036: Pool-Unlock/Deadline Season Model + 3-Tier
--                 Shield Rework
-- ------------------------------------------------------------
-- Depends on: migration_029 (season_points, point_shields,
-- season_point_events, has_active_shield, is_season_points_locked),
-- migration_032 (point_shields.tier column), migration_035
-- (apply_sp_adjustment, nullable season_point_events.match_id —
-- this migration's new event-sourced shield purchase relies on
-- that nullability).
--
-- Replaces the "first to 3500 SP locks the season" model entirely
-- with a two-part model:
--
--   1. POOL-UNLOCK (not a lock): the first player to reach
--      SEASON_POOL_UNLOCK_THRESHOLD (2000, see config.py) flips a
--      permanent per-season flag (seasons.sp_pool_unlocked). This
--      does NOT end the season or freeze anyone's points — the
--      race for rank 1/2/3 continues normally. It only changes the
--      payout FORMULA that applies once the season eventually locks:
--      flat ₹700/500/300 by final rank instead of SP-proportional.
--
--   2. DEADLINE LOCK (the only actual lock trigger now): the season
--      locks when now() >= seasons.end_date, checked LAZILY (no cron
--      needed) from two places in application code — match approval
--      and the points-leaderboard reload button — same "piggyback on
--      an already-frequent action" pattern this codebase already uses
--      for expire_shields(). Whoever holds rank 1/2/3 at that exact
--      moment gets paid, using whichever formula the pool-unlock flag
--      selects.
--
-- Also reworks shields from a flat "any shield = +5win/-3loss becomes
-- +5win/0loss" model into 3 tiers with their own win-bonus and
-- duration (see config.SHIELD_TIERS):
--   - normal:     72h,  +10/win, 0/loss — ₹30 Boost or 150-SP credits
--   - 2x_normal: 144h,  +10/win, 0/loss — ₹50 Boost only (bundle discount)
--   - premium:    72h,  +50/win for the shield's first 24h, then
--                 +10/win — ₹60 Boost only. The day-1 bonus is
--                 APPLIED AUTOMATICALLY here (compared against the
--                 shield's own shield_starts_at), unlike the old
--                 boost_200 tier's day-1 bonus, which required a
--                 manual admin top-up because no per-shield start
--                 timestamp existed to check against at the time.
--                 That constraint no longer applies — point_shields
--                 already carries shield_starts_at for every shield
--                 (needed for duration/expiry regardless), so the
--                 real-time check costs nothing extra to add here.
--
-- Two pre-existing bugs get fixed as a direct consequence of
-- rewriting the functions they live in (not scope creep — leaving
-- them in place while rewriting the same functions would mean
-- knowingly reintroducing them):
--
--   - ChampQueue_Audit_2026-09-13.md §6.2: the credits-path shield
--     purchase deducted SP via a direct UPDATE on season_points.points
--     with no season_point_events row, so any future
--     recompute_player_season_points() call silently refunded it.
--     Fixed by making the purchase event-sourced (a real
--     season_point_events row, delta = -cost, match_id = null — same
--     pattern apply_sp_adjustment already established) AND by moving
--     the whole purchase into one atomic RPC (row-locked balance
--     check + event + cached-total update + shield insert) instead of
--     the old two separate non-atomic Python calls the same audit
--     flagged as a secondary race risk.
--
--   - ChampQueue_Audit_2026-09-13.md §6.3: the season-end announcement
--     had no already-announced guard and would repost on every single
--     subsequent match once the season locked. Fixed with an
--     announced-flag + atomic mark_*_announced() functions (UPDATE
--     ... WHERE flag = false, so only the caller that actually flips
--     it gets told to announce) for BOTH new announcable events
--     (pool-unlock and season-end) — built this way from the start
--     rather than copying the unguarded pattern forward into new code.
--
-- Old objects this migration makes obsolete (kept, not dropped, to
-- avoid breaking anything that still references them by name — see
-- the deprecation notes inline):
--   - lock_season_points(season_id, winner_id)  -> lock_season_points_by_deadline(season_id)
--   - the threshold-check block inside update_season_points_for_match -> check_and_unlock_pool()
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- 1. New columns on `seasons` — the pool-unlock flag and the
--    announcement guards. seasons.end_date ALREADY EXISTS
--    (schema.sql) and was previously unused by any points logic —
--    this migration is what makes it load-bearing. It is NOT set
--    by this migration (deliberately — see the note at the bottom
--    of this file). Until it's set, check_and_lock_season_by_
--    deadline() is a no-op and the season behaves as "still open,
--    no deadline configured yet."
-- ────────────────────────────────────────────────────────────
alter table seasons add column if not exists sp_pool_unlocked boolean not null default false;
alter table seasons add column if not exists sp_pool_unlocked_at timestamptz;
alter table seasons add column if not exists sp_pool_unlock_announced boolean not null default false;
alter table seasons add column if not exists sp_season_end_announced boolean not null default false;

comment on column seasons.sp_pool_unlocked is
    'True once ANY player has reached SEASON_POOL_UNLOCK_THRESHOLD (2000 SP) this season. A ratchet, not a snapshot — never clears itself even if that player later drops back under 2000. Changes the payout FORMULA at lock time (flat 700/500/300 by rank) but does not itself end the season.';
comment on column seasons.sp_season_end_announced is
    'Guards check_and_announce_season_end from reposting on every match after lock — see migration_036 header, audit finding 6.3.';


-- ────────────────────────────────────────────────────────────
-- 2. get_active_shield_tier() — like has_active_shield() but
--    returns the tier + start time instead of a bare boolean, so
--    the points calculation can tell tiers apart (specifically:
--    detect premium's first-24h window). Superset of
--    has_active_shield(); that function is left in place
--    unchanged since nothing about it is wrong, just insufficient
--    for the new tier-aware logic.
-- ────────────────────────────────────────────────────────────
create or replace function get_active_shield_tier(p_player_id bigint, p_season_id bigint)
returns table (tier text, shield_starts_at timestamptz)
language sql
stable
as $$
    select ps.tier, ps.shield_starts_at
    from point_shields ps
    where ps.player_id = p_player_id
      and ps.season_id = p_season_id
      and ps.status = 'active'
      and ps.shield_starts_at <= now()
      and ps.shield_ends_at > now()
    order by ps.shield_starts_at desc
    limit 1;
$$;


-- ────────────────────────────────────────────────────────────
-- 3. purchase_shield_with_credits() — atomic, event-sourced
--    replacement for the old two-call Python-side deduction.
--    Fixes both §6.2 (silent refund on recompute) and the
--    secondary non-atomicity note in the same audit finding.
--    Only ever called for the 'normal' tier in practice (the only
--    tier config.py exposes a credits_cost for) but takes tier/
--    cost/duration as parameters rather than hardcoding — no
--    reason to bake in a rule that lives correctly in config.py.
--
--    `for update` row-locks the season_points row for the
--    duration of this transaction, closing the check-then-deduct
--    race the audit flagged (two concurrent purchase attempts can
--    no longer both pass the balance check against the same stale
--    balance).
--
--    OUT-prefixed return columns: same reason as
--    apply_sp_adjustment (migration_035) — RETURNS TABLE binds
--    positionally, not by the SELECT's own aliases, so the actual
--    JSON keys PostgREST hands back are these OUT names regardless
--    of what's written after `select` in the body. db.py's wrapper
--    normalizes these back to plain names before returning to
--    callers — see purchase_shield_with_credits's Python wrapper.
-- ────────────────────────────────────────────────────────────
create or replace function purchase_shield_with_credits(
    p_player_id bigint,
    p_season_id bigint,
    p_tier text,
    p_cost_points integer,
    p_duration_hours integer
)
returns table (
    out_id bigint,
    out_tier text,
    out_cost_points integer,
    out_shield_starts_at timestamptz,
    out_shield_ends_at timestamptz
)
language plpgsql
as $$
declare
    v_locked boolean;
    v_balance integer;
    v_now timestamptz := now();
    v_shield_id bigint;
begin
    select is_season_points_locked(p_season_id) into v_locked;
    if v_locked then
        raise exception 'Season points are locked for season_id=% — cannot buy a shield', p_season_id;
    end if;

    select points into v_balance
    from season_points
    where season_id = p_season_id and player_id = p_player_id
    for update;

    if v_balance is null or v_balance < p_cost_points then
        raise exception 'Insufficient points';
    end if;

    -- Existing active shield check (one shield at a time, same rule
    -- as before) — belongs here now that this is the single atomic
    -- entry point for the credits path, rather than a separate
    -- Python-side pre-check that could race against this insert.
    if exists (
        select 1 from point_shields
        where player_id = p_player_id and season_id = p_season_id
          and status = 'active' and shield_starts_at <= v_now and shield_ends_at > v_now
    ) then
        raise exception 'Player already has an active shield';
    end if;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    values (p_season_id, p_player_id, null, -p_cost_points, false);

    update season_points
    set points = points - p_cost_points, updated_at = v_now
    where season_id = p_season_id and player_id = p_player_id;

    insert into point_shields (
        season_id, player_id, payment_method, cost_points, tier,
        status, shield_starts_at, shield_ends_at
    ) values (
        p_season_id, p_player_id, 'points', p_cost_points, p_tier,
        'active', v_now, v_now + make_interval(hours => p_duration_hours)
    )
    returning id into v_shield_id;

    return query
        select ps.id, ps.tier, ps.cost_points, ps.shield_starts_at, ps.shield_ends_at
        from point_shields ps
        where ps.id = v_shield_id;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 4. update_season_points_for_match() — REWRITTEN.
--    - Win bonus is now tier-aware (10 for any active shield, 50
--      for premium inside its first 24h) instead of a flat 5.
--    - The old "cross 2500/3500 -> lock" block is GONE. Locking is
--      no longer SP-driven at all (see check_and_lock_season_by_
--      deadline below). This function's only season-level side
--      effect now is check_and_unlock_pool — which flips a flag,
--      never freezes anything.
-- ────────────────────────────────────────────────────────────
create or replace function update_season_points_for_match(
    p_match_id bigint,
    p_season_id bigint
)
returns void
language plpgsql
as $$
declare
    v_already_locked boolean;
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
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0 then
                -- WON. Tier-aware bonus — win_sp/day1_win_sp values
                -- documented in config.SHIELD_TIERS; duplicated here
                -- as literals because SQL has no reach into config.py
                -- (same "duplicated across N copies" class as the
                -- rank-tier table — see engineering-rules' Known
                -- landmines). If either tier's win amounts change in
                -- config.py, this CASE must be updated to match.
                case
                    when gst.tier is null then 5                                    -- no shield
                    when gst.tier = 'premium'
                         and now() < gst.shield_starts_at + interval '24 hours'
                        then 50                                                      -- premium, day 1
                    else 10                                                          -- normal / 2x_normal / premium day 2-3
                end
            else
                -- LOST. All three tiers give 0 on loss, so this stays
                -- a simple "shielded at all?" check.
                case when gst.tier is not null then 0 else -3 end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0
                 and gst.tier is not null
                then true
            else false
        end
    from match_round_results mrr
    left join lateral get_active_shield_tier(mrr.player_id, p_season_id) gst on true
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

    -- Pool-unlock check only — no locking happens here anymore.
    perform check_and_unlock_pool(p_season_id);
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 5. check_and_unlock_pool() — idempotent, race-safe (single
--    UPDATE ... WHERE sp_pool_unlocked = false). Returns true only
--    for the ONE call that actually flips the flag, so callers
--    know whether THEY are the one that should announce it —
--    same pattern used for the announced-guards below.
-- ────────────────────────────────────────────────────────────
create or replace function check_and_unlock_pool(p_season_id bigint)
returns boolean
language plpgsql
as $$
declare
    v_threshold constant integer := 2000;  -- config.SEASON_POOL_UNLOCK_THRESHOLD
    v_qualifies boolean;
    v_flipped boolean;
begin
    select exists (
        select 1 from season_points
        where season_id = p_season_id and points >= v_threshold
    ) into v_qualifies;

    if not v_qualifies then
        return false;
    end if;

    update seasons
    set sp_pool_unlocked = true, sp_pool_unlocked_at = now()
    where id = p_season_id and sp_pool_unlocked = false
    returning true into v_flipped;

    return coalesce(v_flipped, false);
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 6. lock_season_points_by_deadline() — replaces
--    lock_season_points(season_id, winner_id). No "winner" param
--    anymore: rank 1/2/3 are simply whoever holds those positions
--    at the moment this runs, by points desc, earliest-to-reach-
--    that-total as tiebreak (same convention the old threshold
--    check already used for "who got there first").
--    Payout formula branches on seasons.sp_pool_unlocked:
--      unlocked  -> flat 700/500/300 regardless of exact SP
--      never unlocked -> points/5 each, uncapped, no floor to 1500
--        (a low-scoring season can legitimately pay out less than
--        the full pool — confirmed against the user's own example:
--        three players at 1900 SP each -> 380+380+380 = ₹1140).
-- ────────────────────────────────────────────────────────────
create or replace function lock_season_points_by_deadline(p_season_id bigint)
returns void
language plpgsql
as $$
declare
    v_points_to_rupee constant integer := 5;
    v_pool_unlocked boolean;
    v_rank1_id bigint;
    v_rank2_id bigint;
    v_rank3_id bigint;
begin
    select sp_pool_unlocked into v_pool_unlocked from seasons where id = p_season_id;

    update season_points
    set is_locked = true, locked_at = now()
    where season_id = p_season_id;

    select player_id into v_rank1_id
    from season_points
    where season_id = p_season_id
    order by points desc, updated_at asc
    limit 1;

    if v_rank1_id is not null then
        update season_points
        set locked_rank = 1,
            payout_rupees = case when v_pool_unlocked then 700 else points / v_points_to_rupee end
        where season_id = p_season_id and player_id = v_rank1_id;
    end if;

    select player_id into v_rank2_id
    from season_points
    where season_id = p_season_id and player_id is distinct from v_rank1_id
    order by points desc, updated_at asc
    limit 1;

    if v_rank2_id is not null then
        update season_points
        set locked_rank = 2,
            payout_rupees = case when v_pool_unlocked then 500 else points / v_points_to_rupee end
        where season_id = p_season_id and player_id = v_rank2_id;
    end if;

    select player_id into v_rank3_id
    from season_points
    where season_id = p_season_id
      and player_id is distinct from v_rank1_id
      and player_id is distinct from v_rank2_id
    order by points desc, updated_at asc
    limit 1;

    if v_rank3_id is not null then
        update season_points
        set locked_rank = 3,
            payout_rupees = case when v_pool_unlocked then 300 else points / v_points_to_rupee end
        where season_id = p_season_id and player_id = v_rank3_id;
    end if;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 7. check_and_lock_season_by_deadline() — THE lock trigger now.
--    No-op (returns false) if seasons.end_date isn't set yet, or
--    if now() hasn't reached it, or if the season is already
--    locked. Race-safe the same way check_and_unlock_pool is: only
--    the call that actually performs the lock returns true.
--    Call this from anywhere that already runs often — match
--    approval and the points-leaderboard reload button, per the
--    lazy-lock plan. No new scheduled job.
-- ────────────────────────────────────────────────────────────
create or replace function check_and_lock_season_by_deadline(p_season_id bigint)
returns boolean
language plpgsql
as $$
declare
    v_end_date timestamptz;
    v_already_locked boolean;
begin
    select end_date into v_end_date from seasons where id = p_season_id;
    if v_end_date is null or now() < v_end_date then
        return false;
    end if;

    select is_season_points_locked(p_season_id) into v_already_locked;
    if v_already_locked then
        return false;
    end if;

    perform lock_season_points_by_deadline(p_season_id);
    return true;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 8. mark_pool_unlock_announced() / mark_season_end_announced() —
--    atomic announce-once guards (audit finding 6.3). Each is a
--    single UPDATE ... WHERE flag = false, so under concurrent
--    calls only one ever gets true back — exactly the "only the
--    caller that flipped it announces" pattern check_and_unlock_
--    pool / check_and_lock_season_by_deadline already use for the
--    underlying state change itself.
-- ────────────────────────────────────────────────────────────
create or replace function mark_pool_unlock_announced(p_season_id bigint)
returns boolean
language sql
as $$
    update seasons
    set sp_pool_unlock_announced = true
    where id = p_season_id and sp_pool_unlock_announced = false
    returning true;
$$;

create or replace function mark_season_end_announced(p_season_id bigint)
returns boolean
language sql
as $$
    update seasons
    set sp_season_end_announced = true
    where id = p_season_id and sp_season_end_announced = false
    returning true;
$$;


-- ────────────────────────────────────────────────────────────
-- 9. recompute_season_points_for_match() — REWRITTEN to match
--    the new tier-aware delta logic (section 4), and with the
--    obsolete auto-relock/unlock block removed entirely: locking
--    is deadline-driven now, not points-driven, so "did this
--    correction push someone back under the threshold" no longer
--    means anything — there is no threshold that locks. If a
--    match correction happens AFTER the season has already locked
--    by deadline, that's an explicit admin situation to handle by
--    hand (re-running lock_season_points_by_deadline manually if
--    genuinely warranted), not something this repair tool should
--    silently decide on its own — same reasoning apply_sp_
--    adjustment's header already gives for removing its own
--    auto-lock check.
-- ────────────────────────────────────────────────────────────
create or replace function recompute_season_points_for_match(p_match_id bigint)
returns void
language plpgsql
as $$
declare
    v_season_id bigint;
begin
    select season_id into v_season_id
    from matches
    where id = p_match_id;

    if v_season_id is null then
        return;
    end if;

    delete from season_point_events
    where match_id = p_match_id and season_id = v_season_id;

    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        v_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0 then
                case
                    when gst.tier is null then 5
                    when gst.tier = 'premium'
                         and m.completed_at < gst.shield_starts_at + interval '24 hours'
                        then 50
                    else 10
                end
            else
                case when gst.tier is not null then 0 else -3 end
        end,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) <= 0
                 and gst.tier is not null
                then true
            else false
        end
    from match_round_results mrr
    join matches m on m.id = mrr.match_id
    left join lateral get_active_shield_tier(mrr.player_id, v_season_id) gst on true
    where mrr.match_id = p_match_id;

    perform recompute_player_season_points(spe.player_id, v_season_id)
    from (select distinct player_id from season_point_events where match_id = p_match_id and season_id = v_season_id) spe;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 10. Grants — new functions need explicit service_role access,
--     same class of miss this project has hit three times already
--     (migration_030, migration_035 §5). Table-level grants from
--     migration_029/032/035 already cover the columns touched
--     above; only the new FUNCTIONs need this (invoker-rights
--     functions run as whatever role calls them, but Supabase's
--     RPC layer still needs EXECUTE on the function itself).
-- ────────────────────────────────────────────────────────────
grant execute on function get_active_shield_tier(bigint, bigint) to service_role;
grant execute on function purchase_shield_with_credits(bigint, bigint, text, integer, integer) to service_role;
grant execute on function check_and_unlock_pool(bigint) to service_role;
grant execute on function lock_season_points_by_deadline(bigint) to service_role;
grant execute on function check_and_lock_season_by_deadline(bigint) to service_role;
grant execute on function mark_pool_unlock_announced(bigint) to service_role;
grant execute on function mark_season_end_announced(bigint) to service_role;


-- ============================================================
-- DELIBERATELY NOT DONE BY THIS MIGRATION — a human decision,
-- not a default:
--
-- seasons.end_date is NOT set here. Until it is, the season has
-- no deadline and check_and_lock_season_by_deadline() is a
-- permanent no-op — matches carry on being scored (with the new
-- tier-aware shield math) but the season simply never locks. Set
-- it explicitly once the real Oct 10 EOD cutoff (and which
-- timezone it means — IST almost certainly, but this must be
-- converted to the equivalent UTC instant before writing it,
-- since timestamptz stores/compares in UTC regardless of how it's
-- entered) is confirmed:
--
--   update seasons
--   set end_date = '2026-10-10 23:59:59+05:30'  -- adjust if EOD means something other than 23:59:59 IST
--   where id = <season_2_id>;
--
-- Sanity checks to run after applying (adjust ids to real values):
--   select id, name, end_date, sp_pool_unlocked, sp_pool_unlocked_at
--   from seasons where id = <season_2_id>;
--
--   select check_and_unlock_pool(<season_2_id>);        -- should be false unless someone's already >=2000
--   select check_and_lock_season_by_deadline(<season_2_id>);  -- should be false until end_date is set and passed
-- ============================================================
