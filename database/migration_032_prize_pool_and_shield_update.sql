-- ============================================================
-- MIGRATION 032: Prize pool update, shield overhaul, consent flow
-- ------------------------------------------------------------
-- Season 2 tuning based on live testing + economics review:
--   1. Prize pool: ₹1000 → ₹1500 (1st ₹700, 2nd cap ₹500, 3rd cap ₹300)
--   2. Season-end threshold: 2500 → 3500 SP
--   3. Shield duration: 48h → 168h (7 days)
--   4. Shield tiers: ₹100 (500 SP equiv) and ₹200 (1000 SP equiv)
--   5. Consent flow: new 'player_consented' status for cash-path
--   6. Tier tracking on point_shields
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- 1. Add new columns and update constraints on point_shields
-- ────────────────────────────────────────────────────────────

-- Add tier column (credits/boost_100/boost_200)
alter table point_shields add column if not exists tier text;

-- Add consent tracking
alter table point_shields add column if not exists consented_at timestamptz;

-- Drop old status constraint and add new one including 'player_consented'
alter table point_shields drop constraint if exists point_shields_status_check;
alter table point_shields add constraint point_shields_status_check
    check (status in (
        'player_consented',          -- player clicked "I Agree" on consent screen (cash/boost path)
        'pending_hod_confirmation',  -- admin initiated /admin-grant-shield, awaiting HOD
        'active',                    -- shield is live
        'expired',                   -- duration elapsed naturally
        'rejected'                   -- HOD rejected the cash-path request
    ));

-- Update comment on shield_ends_at
comment on column point_shields.shield_ends_at is 'starts_at + 168h (7 days). Was 48h pre-migration_032.';


-- ────────────────────────────────────────────────────────────
-- 2. Redefine update_season_points_for_match() — threshold 2500 → 3500
-- ────────────────────────────────────────────────────────────
create or replace function update_season_points_for_match(
    p_match_id bigint,
    p_season_id bigint
)
returns void
language plpgsql
as $$
declare
    v_threshold constant integer := 3500;  -- was 2500 pre-migration_032
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


-- ────────────────────────────────────────────────────────────
-- 3. Redefine lock_season_points() — new caps (700/500/300)
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
    v_cap_2nd constant integer := 500;   -- was 300 pre-migration_032
    v_cap_3rd constant integer := 300;   -- was 200 pre-migration_032
begin
    -- Step 1: Lock ALL rows
    update season_points
    set is_locked = true,
        locked_at = now()
    where season_id = p_season_id;

    -- Step 2: Assign #1 — ₹700 (was ₹500)
    update season_points
    set locked_rank = 1,
        payout_rupees = 700
    where season_id = p_season_id
      and player_id = p_winner_player_id;

    -- Step 3: Assign #2
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

    -- Step 4: Assign #3
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
-- 5. Fix chk_cash_requires_initiator — allow player_consented rows
--    to have initiated_by = null
-- ────────────────────────────────────────────────────────────
-- Bug: this constraint was written when the ONLY cash-path insert
-- was /admin-grant-shield (status='pending_hod_confirmation'),
-- where initiated_by is always set at insert time. This migration
-- added a new EARLIER insert stage — create_shield_consent(), status
-- ='player_consented' — that fires the moment a player clicks "I
-- Agree", before any admin has touched it. initiated_by is correctly
-- null at that point; the old constraint didn't get updated to know
-- about the new stage and rejected every consent insert outright.
--
-- Caught live 2026-09-07 testing on ebsleroxzikxxvqblzry: every
-- "I Agree" click failed with 'violates check constraint
-- "chk_cash_requires_initiator"' (23514).
--
-- Fix: initiated_by is only required once the row is NOT sitting in
-- the pre-admin 'player_consented' stage.
alter table point_shields drop constraint if exists chk_cash_requires_initiator;
alter table point_shields add constraint chk_cash_requires_initiator check (
    payment_method != 'cash'
    or status = 'player_consented'
    or initiated_by is not null
);


-- ────────────────────────────────────────────────────────────
-- 6. Grant service_role on new columns (defensive)
-- ────────────────────────────────────────────────────────────
-- Not strictly needed (the table-level grant from migration_030
-- covers new columns automatically), but explicit > implicit
-- given the class of bug migration_030 already fixed once.
grant select, insert, update, delete on public.point_shields to service_role;