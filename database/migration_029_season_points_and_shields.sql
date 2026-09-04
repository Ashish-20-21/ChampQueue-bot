-- ============================================================
-- MIGRATION 029: Season Points & Shield Powers
-- ------------------------------------------------------------
-- Depends on: migration_023 (season activation — seasons table
-- with is_active, code column; matches.season_id backfilled).
--
-- Adds:
--   1. season_points          — running point totals per player per season
--   2. point_shields           — shield purchase/grant audit trail
--   3. season_point_events     — per-match point change log (audit + recompute source)
--   4. update_season_points()  — called inside approve_match transaction
--   5. recompute_player_season_points()  — admin repair tool (single player)
--   6. recompute_all_season_points()     — admin repair tool (full season)
--   7. season_points_leaderboard()       — ranked leaderboard RPC
--   8. Updated approve_match()  — adds points CTE after MMR commit
--
-- Points rules:
--   - Win = +5, Loss = -3 (no MVP bonus)
--   - Floor at 0 (never negative)
--   - Active shield suppresses loss penalty (0 instead of -3)
--   - First player to reach >=2500 locks the season
--   - Prize: 1st=₹500, 2nd=min(pts÷5, ₹300), 3rd=min(pts÷5, ₹200)
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- 1. season_points — one row per player per season
-- ────────────────────────────────────────────────────────────
create table if not exists season_points (
    id              bigserial primary key,
    season_id       bigint not null references seasons(id),
    player_id       bigint not null references players(id),
    points          integer not null default 0,
    is_locked       boolean not null default false,  -- true once season ends for this snapshot
    locked_at       timestamptz,                     -- when the season-end snapshot froze this row
    locked_rank     integer,                         -- 1/2/3/null — prize position at lock time
    payout_rupees   integer,                         -- calculated payout at lock time (null if no prize)
    updated_at      timestamptz not null default now(),
    unique (season_id, player_id)
);

create index if not exists idx_season_points_season on season_points(season_id);
create index if not exists idx_season_points_player on season_points(player_id);
create index if not exists idx_season_points_ranking on season_points(season_id, points desc);


-- ────────────────────────────────────────────────────────────
-- 2. season_point_events — per-match audit trail
-- ────────────────────────────────────────────────────────────
-- Every point change gets a row here. recompute reads FROM this
-- table (not match_round_results directly) so corrections that
-- change a player's win/loss status also need to update/delete
-- the corresponding event row — same pattern as the existing
-- match_round_results -> recompute_player_career_stats flow.
create table if not exists season_point_events (
    id              bigserial primary key,
    season_id       bigint not null references seasons(id),
    player_id       bigint not null references players(id),
    match_id        bigint not null references matches(id) on delete cascade,
    delta           integer not null,          -- +5 or -3 or 0 (shielded loss)
    was_shielded    boolean not null default false,
    created_at      timestamptz not null default now(),
    unique (season_id, player_id, match_id)    -- one event per player per match per season
);

create index if not exists idx_season_point_events_match on season_point_events(match_id);


-- ────────────────────────────────────────────────────────────
-- 3. point_shields — full audit trail for every shield ever
-- ────────────────────────────────────────────────────────────
create table if not exists point_shields (
    id                  bigserial primary key,
    season_id           bigint not null references seasons(id),
    player_id           bigint not null references players(id),

    -- Payment
    payment_method      text not null check (payment_method in ('points', 'cash')),
    cost_points         integer,               -- 500 for points path, null for cash
    cost_rupees         integer,               -- 100 for cash path, null for points

    -- Two-approval chain (cash path only)
    initiated_by        text,                  -- admin discord_id who ran /admin-grant-shield
    initiated_at        timestamptz,
    confirmed_by        text,                  -- HOD discord_id who clicked Confirm
    confirmed_at        timestamptz,
    rejected_by         text,                  -- HOD discord_id who clicked Reject (if rejected)
    rejected_at         timestamptz,

    -- Status lifecycle
    status              text not null default 'active'
                        check (status in (
                            'pending_hod_confirmation',  -- cash path: admin initiated, awaiting HOD
                            'active',                    -- shield is live (points path: immediate; cash: after HOD confirm)
                            'expired',                   -- 48h elapsed naturally
                            'rejected'                   -- HOD rejected the cash-path request
                        )),

    -- Shield window
    shield_starts_at    timestamptz,           -- null while pending; set on activation
    shield_ends_at      timestamptz,           -- starts_at + 48h

    created_at          timestamptz not null default now(),

    -- Cash path integrity: if payment_method is 'cash', initiated_by MUST be set
    constraint chk_cash_requires_initiator check (
        payment_method != 'cash' or initiated_by is not null
    ),
    -- Cash path: confirmed_by must differ from initiated_by (two-person rule)
    constraint chk_two_person_approval check (
        confirmed_by is null or initiated_by is null or confirmed_by != initiated_by
    )
);

create index if not exists idx_point_shields_active on point_shields(player_id, season_id)
    where status = 'active';
create index if not exists idx_point_shields_pending on point_shields(season_id)
    where status = 'pending_hod_confirmation';


-- ────────────────────────────────────────────────────────────
-- 4. Helper: check if a player has an active shield right now
-- ────────────────────────────────────────────────────────────
create or replace function has_active_shield(p_player_id bigint, p_season_id bigint)
returns boolean
language sql
stable
as $$
    select exists (
        select 1 from point_shields
        where player_id = p_player_id
          and season_id = p_season_id
          and status = 'active'
          and shield_starts_at <= now()
          and shield_ends_at > now()
    );
$$;


-- ────────────────────────────────────────────────────────────
-- 5. Helper: check if season points are frozen
-- ────────────────────────────────────────────────────────────
create or replace function is_season_points_locked(p_season_id bigint)
returns boolean
language sql
stable
as $$
    select exists (
        select 1 from season_points
        where season_id = p_season_id
          and is_locked = true
        limit 1
    );
$$;


-- ────────────────────────────────────────────────────────────
-- 6. update_season_points_for_match()
--    Called from approve_match after MMR is committed.
--    For each player in the match: derive win/loss, check
--    shield, apply +5/-3/0, upsert season_points, log event.
--    Then check if anyone just crossed 2500 -> lock season.
-- ────────────────────────────────────────────────────────────
create or replace function update_season_points_for_match(
    p_match_id bigint,
    p_season_id bigint
)
returns void
language plpgsql
as $$
declare
    v_threshold constant integer := 2500;
    v_already_locked boolean;
    v_first_player_id bigint;
    v_first_points integer;
begin
    -- Skip if season is already locked (points frozen)
    select is_season_points_locked(p_season_id) into v_already_locked;
    if v_already_locked then
        return;
    end if;

    -- For each player in this match: determine win/loss from
    -- match_round_results using the same MVP-stripped signal
    -- recompute_player_career_stats trusts. Then check shield
    -- status and compute the delta.
    --
    -- Win/loss derivation:
    --   base_delta = mmr_delta - (5 if is_mvp else 0)
    --   base_delta > 0  => won  => +5 points
    --   base_delta <= 0 => lost => -3 points (or 0 if shielded)
    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        p_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0
                then 5   -- won
            else
                case
                    when has_active_shield(mrr.player_id, p_season_id)
                        then 0    -- lost but shielded
                    else -3       -- lost, no shield
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

    -- Upsert season_points: create row if first match, or add delta.
    -- The ON CONFLICT DO UPDATE uses the EVENT's delta (looked up from
    -- the just-inserted season_point_events row), not excluded.points,
    -- since excluded.points is the insert-attempt value (for new rows)
    -- and we need the additive delta for existing rows.
    insert into season_points (season_id, player_id, points, updated_at)
    select
        p_season_id,
        spe.player_id,
        greatest(0, spe.delta),  -- new-row value (floor at 0)
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

    -- ── Season-end check ──
    -- Did anyone just cross the threshold?
    select sp.player_id, sp.points
    into v_first_player_id, v_first_points
    from season_points sp
    where sp.season_id = p_season_id
      and sp.points >= v_threshold
      and sp.is_locked = false
    order by sp.points desc, sp.updated_at asc
    limit 1;

    if v_first_player_id is not null then
        -- Lock the ENTIRE season's points table
        perform lock_season_points(p_season_id, v_first_player_id);
    end if;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 7. lock_season_points()
--    Freezes all points, assigns ranks 1/2/3, calculates payouts.
-- ────────────────────────────────────────────────────────────
create or replace function lock_season_points(
    p_season_id bigint,
    p_winner_player_id bigint
)
returns void
language plpgsql
as $$
declare
    v_points_to_rupee constant integer := 5;
    v_cap_2nd constant integer := 300;
    v_cap_3rd constant integer := 200;
begin
    -- Step 1: Lock ALL rows (everyone's points freeze)
    update season_points
    set is_locked = true,
        locked_at = now()
    where season_id = p_season_id;

    -- Step 2: Assign #1 — the player who crossed 2500
    update season_points
    set locked_rank = 1,
        payout_rupees = 500
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


-- ────────────────────────────────────────────────────────────
-- 8. recompute_player_season_points() — admin repair tool
--    Derives true total from season_point_events, replaces
--    the cached season_points row. Does NOT re-derive events
--    from match_round_results — that's a separate step if the
--    events themselves are wrong (delete bad event, re-run
--    update_season_points_for_match for that match).
-- ────────────────────────────────────────────────────────────
create or replace function recompute_player_season_points(
    p_player_id bigint,
    p_season_id bigint
)
returns void
language plpgsql
as $$
declare
    v_total integer;
begin
    select coalesce(sum(delta), 0)
    into v_total
    from season_point_events
    where player_id = p_player_id
      and season_id = p_season_id;

    v_total := greatest(0, v_total);

    insert into season_points (season_id, player_id, points, updated_at)
    values (p_season_id, p_player_id, v_total, now())
    on conflict (season_id, player_id) do update
        set points = v_total,
            updated_at = now();
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 9. recompute_all_season_points() — nuclear admin repair
--    Re-derives every player's points for a season from events.
-- ────────────────────────────────────────────────────────────
create or replace function recompute_all_season_points(p_season_id bigint)
returns void
language plpgsql
as $$
declare
    r record;
begin
    for r in
        select distinct player_id
        from season_point_events
        where season_id = p_season_id
    loop
        perform recompute_player_season_points(r.player_id, p_season_id);
    end loop;
end;
$$;


-- ────────────────────────────────────────────────────────────
-- 10. Recompute from scratch — re-derive events from
--     match_round_results then recompute totals.
--     Used when the events themselves might be wrong
--     (e.g. a match correction changed win/loss outcome).
-- ────────────────────────────────────────────────────────────
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

    -- Delete old events for this match
    delete from season_point_events
    where match_id = p_match_id and season_id = v_season_id;

    -- Re-insert events from match_round_results
    -- (same logic as update_season_points_for_match but for
    -- a match that's already completed)
    insert into season_point_events (season_id, player_id, match_id, delta, was_shielded)
    select
        v_season_id,
        mrr.player_id,
        p_match_id,
        case
            when (mrr.mmr_delta - (case when mrr.is_mvp then 5 else 0 end)) > 0
                then 5
            else
                -- For recompute: check if shield was active at match completion time
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

    -- Now recompute each affected player's total
    perform recompute_player_season_points(spe.player_id, v_season_id)
    from (select distinct player_id from season_point_events where match_id = p_match_id and season_id = v_season_id) spe;

    -- If season was locked and recompute changed the outcome, unlock it
    -- and flag for admin review (the Python layer handles the alert)
    if v_locked then
        -- Check if the original #1 still qualifies
        if not exists (
            select 1 from season_points
            where season_id = v_season_id
              and locked_rank = 1
              and points >= 2500
        ) then
            -- Unlock the season — admin must review
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


-- ────────────────────────────────────────────────────────────
-- 11. season_points_leaderboard() — for the leaderboard channel
-- ────────────────────────────────────────────────────────────
create or replace function season_points_leaderboard(p_season_id bigint)
returns table (
    rank bigint,
    player_id bigint,
    discord_id text,
    ign text,
    points integer,
    is_locked boolean,
    locked_rank integer,
    payout_rupees integer
)
language sql
stable
as $$
    select
        row_number() over (order by sp.points desc, sp.updated_at asc),
        sp.player_id,
        p.discord_id,
        p.ign,
        sp.points,
        sp.is_locked,
        sp.locked_rank,
        sp.payout_rupees
    from season_points sp
    join players p on p.id = sp.player_id
    where sp.season_id = p_season_id
      and sp.points > 0
    order by sp.points desc, sp.updated_at asc;
$$;


-- ────────────────────────────────────────────────────────────
-- 12. Updated approve_match() — adds points call after MMR
-- ────────────────────────────────────────────────────────────
-- This replaces the existing approve_match from migration_015.
-- The ONLY addition is the update_season_points_for_match call
-- at the end, inside the same transaction. Everything else
-- (row-count assertion, MMR update, rank bands, peak tracking,
-- match_players update, status commit) is copied VERBATIM from
-- migration_015 — do not modify those sections here.
create or replace function approve_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language plpgsql
as $$
declare
    v_match matches%rowtype;
    v_season_id bigint;
begin
    select * into v_match from matches where id = p_match_id for update;
    if not found then
        raise exception 'match % does not exist', p_match_id;
    end if;
    if v_match.status <> 'pending_verification' then
        raise exception 'match % is not pending verification', p_match_id;
    end if;
    if (select count(*) from match_round_results where match_id = p_match_id) <> 10 then
        raise exception 'match % must have exactly 10 round-result rows', p_match_id;
    end if;

    -- Capture season_id for points update
    v_season_id := v_match.season_id;

    return query
    with deltas as (
        select mrr.player_id, sum(mrr.mmr_delta)::integer as total_delta
        from match_round_results mrr
        where mrr.match_id = p_match_id
        group by mrr.player_id
    ), updated_players as (
        update players p
        set mmr = greatest(0, p.mmr + d.total_delta),
            peak_mmr = greatest(p.peak_mmr, greatest(0, p.mmr + d.total_delta)),
            -- current_division dropped in migration_016 — do not
            -- write to it, the column no longer exists. See
            -- migration_031 for the incident this caused live.
            current_rank = case
                when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans'
                when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1'
                when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1'
                when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1'
                when greatest(0, p.mmr + d.total_delta) >= 601  then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1'
                when greatest(0, p.mmr + d.total_delta) >= 201  then 'Elite2'
                else 'Elite1'
            end,
            peak_rank = case when greatest(0, p.mmr + d.total_delta) > p.peak_mmr then case
                when greatest(0, p.mmr + d.total_delta) >= 2001 then 'Titans'
                when greatest(0, p.mmr + d.total_delta) >= 1801 then 'Legendary2'
                when greatest(0, p.mmr + d.total_delta) >= 1601 then 'Legendary1'
                when greatest(0, p.mmr + d.total_delta) >= 1401 then 'Grandmaster2'
                when greatest(0, p.mmr + d.total_delta) >= 1201 then 'Grandmaster1'
                when greatest(0, p.mmr + d.total_delta) >= 1001 then 'Master2'
                when greatest(0, p.mmr + d.total_delta) >= 801  then 'Master1'
                when greatest(0, p.mmr + d.total_delta) >= 601  then 'PRO2'
                when greatest(0, p.mmr + d.total_delta) >= 401  then 'PRO1'
                when greatest(0, p.mmr + d.total_delta) >= 201  then 'Elite2'
                else 'Elite1' end
                else p.peak_rank end,
            updated_at = now()
        from deltas d
        where p.id = d.player_id
        returning p.id, p.mmr - d.total_delta as before_mmr, p.mmr as after_mmr, d.total_delta
    ), updated_match_players as (
        update match_players mp
        set mmr_before = up.before_mmr, mmr_after = up.after_mmr, mmr_change = up.total_delta
        from updated_players up
        where mp.match_id = p_match_id and mp.player_id = up.id
        returning up.id, up.before_mmr, up.after_mmr, up.total_delta
    )
    select * from updated_match_players;

    -- Commit match status
    update matches
    set status = 'completed', completed_at = now(), approved_by = p_approved_by, approved_at = now()
    where id = p_match_id;

    -- ── Season points (new in migration_029) ──
    -- Only fire if this match has a season_id. Matches from before
    -- migration_023 (season backfill) or test matches without a
    -- season_id simply skip this — no points, no error.
    if v_season_id is not null then
        perform update_season_points_for_match(p_match_id, v_season_id);
    end if;
end;
$$;

-- Keep backward-compat alias (same as migration_015)
create or replace function approve_ro3_match(p_match_id bigint, p_approved_by bigint)
returns table (player_id bigint, mmr_before integer, mmr_after integer, mmr_change integer)
language sql as $$
    select * from approve_match(p_match_id, p_approved_by);
$$;


-- ────────────────────────────────────────────────────────────
-- 13. Shield expiry helper — called periodically or on-demand
--     to flip active shields past their 48h window to 'expired'.
--     Not strictly required (has_active_shield checks timestamps
--     directly), but keeps the status column honest for queries
--     and the audit channel.
-- ────────────────────────────────────────────────────────────
create or replace function expire_shields()
returns integer
language plpgsql
as $$
declare
    v_count integer;
begin
    update point_shields
    set status = 'expired'
    where status = 'active'
      and shield_ends_at <= now();
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;