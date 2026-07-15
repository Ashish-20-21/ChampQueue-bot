-- P4: AFK handling, admin MMR adjustment log, abandoned-match cleanup sweep
-- Additive only — safe to run against the live Supabase instance.

-- ------------------------------------------------------------------
-- Allow matches to be marked abandoned (host/player went AFK, scrapped
-- by an admin). Existing status values untouched.
-- ------------------------------------------------------------------
alter table matches
    drop constraint if exists matches_status_check;

alter table matches
    add constraint matches_status_check
    check (status in (
        'forming',
        'map_vote',
        'awaiting_room',
        'in_progress',
        'awaiting_result',
        'awaiting_review',
        'completed',
        'cancelled',
        'abandoned'          -- new: scrapped via /admin-scrap-match after an AFK report
    ));

-- Timestamp for the cleanup sweep to know when a text channel from an
-- abandoned (or completed) match is due for deletion. NULL = not scheduled.
alter table matches
    add column if not exists cleanup_at timestamptz;

-- Lets a room-code correction (+updateroomcode) edit the existing
-- #s-rank-match-logs entry in place instead of posting a duplicate.
alter table matches
    add column if not exists match_log_message_id text;

-- ------------------------------------------------------------------
-- Admin MMR adjustment log — mirrors reputation_log's shape. Keeps a
-- traceable record of every admin-issued MMR change (delta, reason,
-- who/when), separate from match-driven MMR changes in match_players,
-- so a jump in /profile or /rank-progress is never unexplained.
-- ------------------------------------------------------------------
create table if not exists mmr_adjustment_log (
    id          bigserial primary key,
    player_id   bigint not null references players(id) on delete cascade,
    delta       integer not null,
    reason      text not null,
    adjusted_by text not null,   -- Discord ID of the admin who ran the command
    created_at  timestamptz not null default now()
);

create index if not exists idx_mmr_adjustment_log_player on mmr_adjustment_log(player_id);
