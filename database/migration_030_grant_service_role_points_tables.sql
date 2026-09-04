-- ============================================================
-- MIGRATION 030: Grant service_role privileges (hotfix for 029)
-- ------------------------------------------------------------
-- migration_029 created season_points, point_shields, and
-- season_point_events but never granted service_role access to
-- them — same class of miss migration_018 already had to fix
-- once for ign_change_history. Supabase's service_role doesn't
-- automatically get table access on CREATE TABLE; every other
-- table in this schema has an explicit grant somewhere, this one
-- just got missed.
--
-- Caught live 2026-09-03 testing on ebsleroxzikxxvqblzry:
-- `permission denied for table season_points` (42501) from
-- season_points_leaderboard() RPC.
-- ============================================================

grant select, insert, update, delete on public.season_points to service_role;
grant usage, select on sequence public.season_points_id_seq to service_role;

grant select, insert, update, delete on public.point_shields to service_role;
grant usage, select on sequence public.point_shields_id_seq to service_role;

grant select, insert, update, delete on public.season_point_events to service_role;
grant usage, select on sequence public.season_point_events_id_seq to service_role;

-- Functions defined with default (invoker) rights already run as
-- whatever role calls them (service_role, via supabase-py), so no
-- separate GRANT EXECUTE is needed for the RPC functions themselves
-- — the 403 was purely the underlying table grant, confirmed by the
-- error being on `season_points_leaderboard`'s SELECT against
-- season_points, not a permission-to-call-the-function error.
