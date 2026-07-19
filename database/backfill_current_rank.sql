-- One-time backfill: bring players.current_rank / peak_rank in line with
-- the new 150-point tier bands, for every player whose stored rank
-- predates this migration (test-seeded data, or approvals that happened
-- under the old 100-point bands).
--
-- Not required for correctness — region_leaderboard() and
-- player_stats_card() both derive rank live from mmr and never trust
-- this column (see migration_007 + utils/embeds.py, 2026-07-19). This is
-- pure cosmetic cleanup so the RAW players table also reads correctly if
-- anyone queries it directly (e.g. digest.py, or manual admin lookups in
-- the Supabase table view).
--
-- Safe to run any time, any number of times — it's a plain derive-and-
-- set, not additive. Run once now; no need to re-run after this unless
-- the tier bands change again (in which case re-run this exact query).

update players
set current_rank = case
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
    end,
    peak_rank = case
        when greatest(0, peak_mmr) >= 1501 then 'Titans'
        when greatest(0, peak_mmr) >= 1351 then 'Legendary2'
        when greatest(0, peak_mmr) >= 1201 then 'Legendary1'
        when greatest(0, peak_mmr) >= 1051 then 'Grandmaster2'
        when greatest(0, peak_mmr) >= 901  then 'Grandmaster1'
        when greatest(0, peak_mmr) >= 751  then 'Master2'
        when greatest(0, peak_mmr) >= 601  then 'Master1'
        when greatest(0, peak_mmr) >= 451  then 'PRO2'
        when greatest(0, peak_mmr) >= 301  then 'PRO1'
        when greatest(0, peak_mmr) >= 151  then 'Elite2'
        else 'Elite1'
    end;

-- Sanity check after running — every row's current_rank should now
-- agree with what its mmr maps to. Should return zero rows:
--
-- select id, ign, mmr, current_rank from players
-- where current_rank <> (
--     case
--         when greatest(0, mmr) >= 1501 then 'Titans'
--         when greatest(0, mmr) >= 1351 then 'Legendary2'
--         when greatest(0, mmr) >= 1201 then 'Legendary1'
--         when greatest(0, mmr) >= 1051 then 'Grandmaster2'
--         when greatest(0, mmr) >= 901  then 'Grandmaster1'
--         when greatest(0, mmr) >= 751  then 'Master2'
--         when greatest(0, mmr) >= 601  then 'Master1'
--         when greatest(0, mmr) >= 451  then 'PRO2'
--         when greatest(0, mmr) >= 301  then 'PRO1'
--         when greatest(0, mmr) >= 151  then 'Elite2'
--         else 'Elite1'
--     end
-- );
