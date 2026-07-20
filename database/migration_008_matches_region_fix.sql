-- Fix: matches.region did not exist. Found live 2026-07-20 — /match-submit
-- crashed with KeyError: 'region' the moment the P6 region-aware
-- upload/approval channel check tried to read match["region"]. Root
-- cause: region was assumed to already exist on matches (every match's
-- players are all from one region, by the queue's own region-lock rule),
-- but it was only ever a column on players, never copied onto matches
-- itself at creation time.
--
-- Fix is two parts: this migration (schema + backfill), plus a paired
-- code change (database/db.py's create_match() now takes a region
-- argument and stores it; cogs/queue.py's _start_match_flow passes its
-- own `region` parameter through, which was already available there —
-- it just wasn't being threaded into create_match()).

alter table matches add column if not exists region text;

-- Backfill existing matches from their own roster. Safe because every
-- match's 10 players are always same-region (queue never mixes East/West
-- — enforced by DECISIONS.md's region-lock rule), so picking any one
-- player's region via match_players is unambiguous per match.
update matches m
set region = (
    select p.region
    from match_players mp
    join players p on p.id = mp.player_id
    where mp.match_id = m.id
    limit 1
)
where m.region is null;

-- Constrain to the same two values as players.region, and prevent this
-- gap from recurring silently — any future INSERT that forgets region
-- now fails loudly instead of leaving another null time bomb.
alter table matches alter column region set not null;
alter table matches drop constraint if exists matches_region_check;
alter table matches add constraint matches_region_check check (region in ('East', 'West'));

-- Sanity check after running — should return 0. If it doesn't, some
-- match has no match_players rows at all (shouldn't be possible given
-- match_players are written at the same time as the match, but if this
-- returns rows, investigate those specific match ids before assuming
-- the NOT NULL constraint above will apply cleanly.
-- select id, match_id from matches where region is null;
