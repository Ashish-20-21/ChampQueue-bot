-- Champion's Queue — Supabase/Postgres schema
-- Run this in the Supabase SQL editor (or via `psql`) before starting the bot.

-- ============================================================
-- PLAYERS
-- ============================================================
create table if not exists players (
    id                bigserial primary key,
    discord_id        text unique not null,
    cod_uid           text unique not null,        -- permanent identity anchor
    ign               text not null,                -- mutable display name
    region            text not null,
    organization      text,
    status            text not null default 'pending'
                        check (status in ('pending', 'approved', 'rejected', 'banned')),
    approved_by        text,
    approved_at        timestamptz,

    -- skill / ranking
    mmr               integer not null default 1000,
    peak_mmr          integer not null default 1000,
    current_rank      text not null default 'Elite',
    current_division  text not null default 'II',
    peak_rank         text not null default 'Elite',

    -- trust
    reputation        integer not null default 100,   -- 0-100 scale

    -- career aggregates (denormalized for fast profile reads;
    -- recomputed by the stats service after every match)
    total_matches     integer not null default 0,
    wins              integer not null default 0,
    losses            integer not null default 0,
    mvp_count         integer not null default 0,
    avg_kills         numeric(6,2) not null default 0,
    avg_deaths        numeric(6,2) not null default 0,
    avg_damage        numeric(8,2) not null default 0,
    avg_hill_time     numeric(6,2) not null default 0,

    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index if not exists idx_players_discord_id on players(discord_id);
create index if not exists idx_players_status on players(status);
create index if not exists idx_players_mmr on players(mmr desc);

-- ============================================================
-- SEASONS
-- ============================================================
create table if not exists seasons (
    id            bigserial primary key,
    name          text not null,
    start_date    timestamptz not null default now(),
    end_date      timestamptz,
    is_active     boolean not null default true
);

-- ============================================================
-- QUEUE
-- ============================================================
create table if not exists queue_entries (
    id            bigserial primary key,
    player_id     bigint not null references players(id) on delete cascade,
    joined_at     timestamptz not null default now(),
    status        text not null default 'waiting'
                    check (status in ('waiting', 'matched', 'left', 'timed_out')),
    unique (player_id, status) -- a player can only have ONE active 'waiting' row at a time (enforced in app logic too)
);

create index if not exists idx_queue_status on queue_entries(status);

-- ============================================================
-- MATCHES
-- ============================================================
create table if not exists matches (
    id                bigserial primary key,
    match_id          text unique not null,          -- human-facing short ID, e.g. CQ-0001
    season_id         bigint references seasons(id),
    map               text,
    status            text not null default 'forming'
                        check (status in (
                            'forming',       -- team balance / captain / skill vote in progress
                            'map_vote',
                            'awaiting_room', -- room code not yet shared
                            'in_progress',
                            'awaiting_result',
                            'awaiting_review', -- flagged for admin review
                            'completed',
                            'cancelled'
                        )),
    is_bootstrap      boolean not null default false, -- true = random assignment phase, excluded/weighted differently in analysis
    team_a_captain_id bigint references players(id),
    team_b_captain_id bigint references players(id),
    winner_team       text check (winner_team in ('A', 'B')),
    final_score       text,                            -- e.g. "250-210"
    mvp_player_id     bigint references players(id),
    room_code         text,
    room_code_shared_by bigint references players(id),
    text_channel_id   text,
    voice_channel_a_id text,
    voice_channel_b_id text,
    scoreboard_image_url text,
    raw_extraction    jsonb,                            -- raw Vision AI output, kept for audits
    created_at        timestamptz not null default now(),
    completed_at      timestamptz
);

create index if not exists idx_matches_status on matches(status);
create index if not exists idx_matches_match_id on matches(match_id);

-- ============================================================
-- MATCH PLAYERS (per-player, per-match stat line)
-- ============================================================
create table if not exists match_players (
    id             bigserial primary key,
    match_id       bigint not null references matches(id) on delete cascade,
    player_id      bigint not null references players(id),
    team           text not null check (team in ('A', 'B')),
    is_captain     boolean not null default false,
    operator_skill text,

    -- extracted / confirmed stats
    kills          integer,
    deaths         integer,
    assists        integer,
    damage         integer,
    hill_time      numeric(6,2),
    impact         numeric(6,2),
    score          integer,

    is_mvp         boolean not null default false,
    mmr_before     integer,
    mmr_after      integer,
    mmr_change     integer,

    -- integrity
    stat_flagged   boolean not null default false,
    stat_confirmed boolean not null default false,

    unique (match_id, player_id)
);

create index if not exists idx_match_players_match on match_players(match_id);
create index if not exists idx_match_players_player on match_players(player_id);

-- ============================================================
-- OPERATOR SKILL VOTES (per match, per team; unique skill per team enforced in app logic)
-- ============================================================
create table if not exists operator_skill_votes (
    id          bigserial primary key,
    match_id    bigint not null references matches(id) on delete cascade,
    player_id   bigint not null references players(id),
    team        text not null check (team in ('A', 'B')),
    skill       text not null,
    voted_at    timestamptz not null default now(),
    unique (match_id, player_id)
);

-- ============================================================
-- MAP VOTES (round of 3 candidates, one match-wide vote)
-- ============================================================
create table if not exists map_votes (
    id          bigserial primary key,
    match_id    bigint not null references matches(id) on delete cascade,
    player_id   bigint not null references players(id),
    map         text not null,
    voted_at    timestamptz not null default now(),
    unique (match_id, player_id)
);

-- ============================================================
-- REPUTATION LOG (audit trail; players.reputation is the running total)
-- ============================================================
create table if not exists reputation_log (
    id          bigserial primary key,
    player_id   bigint not null references players(id) on delete cascade,
    delta       integer not null,
    reason      text not null,   -- 'afk', 'rage_quit', 'toxicity', 'fake_submission', 'match_dodge', 'admin_adjustment'
    match_id    bigint references matches(id),
    created_at  timestamptz not null default now()
);

-- ============================================================
-- ACHIEVEMENTS
-- ============================================================
create table if not exists achievements (
    id            bigserial primary key,
    code          text unique not null,     -- e.g. 'first_win', 'hill_king'
    name          text not null,
    description   text not null,
    category      text not null check (category in ('general', 'hardpoint', 'streak', 'seasonal'))
);

create table if not exists player_achievements (
    id              bigserial primary key,
    player_id       bigint not null references players(id) on delete cascade,
    achievement_id  bigint not null references achievements(id),
    season_id       bigint references seasons(id),  -- null for permanent/general achievements
    earned_at       timestamptz not null default now(),
    unique (player_id, achievement_id, season_id)
);

-- ============================================================
-- HALL OF FAME (per completed season)
-- ============================================================
create table if not exists hall_of_fame (
    id          bigserial primary key,
    season_id   bigint not null references seasons(id),
    category    text not null,   -- 'champion', 'highest_mmr', 'highest_kd', 'highest_damage', 'best_objective', 'most_mvps'
    player_id   bigint not null references players(id),
    value       text,            -- display value, e.g. "2.14 KD" or "1842 MMR"
    unique (season_id, category)
);

-- ============================================================
-- SEED DATA: achievements
-- ============================================================
insert into achievements (code, name, description, category) values
    ('first_win', 'First Win', 'Win your first official match', 'general'),
    ('matches_100', '100 Matches', 'Play 100 official matches', 'general'),
    ('kills_500', '500 Kills', 'Reach 500 career kills', 'general'),
    ('hill_king', 'Hill King', 'Highest hill time in a match', 'hardpoint'),
    ('rotation_master', 'Rotation Master', 'Fastest average rotation across a match (tracked via impact/hill-time ratio)', 'hardpoint'),
    ('anchor', 'Anchor', 'Most hill time on your team while holding a positive KD', 'hardpoint'),
    ('break_specialist', 'Break Specialist', 'High impact in the final 60 seconds of a hill window (manual/admin tag until detection is automated)', 'hardpoint'),
    ('win_streak_10', '10 Win Streak', 'Win 10 official matches in a row', 'streak'),
    ('positive_kd_streak', 'Positive KD Streak', 'Maintain a positive KD across 5 consecutive matches', 'streak'),
    ('mvp_streak', 'MVP Streak', 'Earn MVP in 3 consecutive matches', 'streak'),
    ('season_champion', 'Season Champion', '#1 MMR at season end', 'seasonal'),
    ('top_10', 'Top 10', 'Finish a season in the top 10 leaderboard', 'seasonal'),
    ('highest_damage_season', 'Highest Damage', 'Highest average damage for a season', 'seasonal'),
    ('most_mvps_season', 'Most MVPs', 'Most MVP awards in a season', 'seasonal'),
    ('best_objective_season', 'Best Objective Player', 'Highest average hill time for a season', 'seasonal')
on conflict (code) do nothing;

-- ============================================================
-- SEED DATA: an active season
-- ============================================================
insert into seasons (name, is_active)
select 'Season 1', true
where not exists (select 1 from seasons where is_active = true);
