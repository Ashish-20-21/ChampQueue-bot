-- migration_018_ign_history_and_entry_method.sql
-- 2026-08-15: two independent schema additions for slash-command features.
--
-- 1. ign_change_history — rate-limit tracking for the new /ign-change
--    self-service command (2 per rolling 7-day window for non-admin players).
--    Also serves as an audit trail, replacing the previous "in-channel
--    message IS the audit trail" approach (2026-08-08 decision, now
--    superseded since players can self-serve and won't always be in the
--    admin channel).
--
-- 2. matches.entry_method — flags whether a match's data came from the
--    normal OCR pipeline ('ocr') or was manually entered by an admin
--    via /admin-enter-result ('manual'). The verification card surfaces
--    this visually so approvers know what they're looking at.

-- ── 1. ign_change_history ───────────────────────────────────────
create table if not exists ign_change_history (
    id          bigserial primary key,
    player_id   bigint not null references players(id) on delete cascade,
    old_ign     text not null,
    new_ign     text not null,
    changed_by  text not null,      -- 'self' for player-initiated, discord_id string for admin
    changed_at  timestamptz not null default now()
);

create index if not exists idx_ign_change_history_player_recent
    on ign_change_history (player_id, changed_at desc);

-- Explicit grants — Supabase does NOT auto-grant a newly created table
-- (or its bigserial-backed sequence) to service_role the way it does
-- for tables created through the dashboard UI. Found live 2026-08-19:
-- table grant alone wasn't enough — bigserial's backing sequence
-- (ign_change_history_id_seq) needs its own separate grant, since
-- Postgres treats table and sequence privileges independently. Both
-- included here so this never has to be discovered live again.
grant select, insert on public.ign_change_history to service_role;
grant usage, select on sequence public.ign_change_history_id_seq to service_role;

-- ── 2. matches.entry_method ─────────────────────────────────────
-- Safe to run even if the column already exists (DO block handles it).
do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_name = 'matches' and column_name = 'entry_method'
    ) then
        alter table matches add column entry_method text not null default 'ocr';
    end if;
end
$$;