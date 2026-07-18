-- ============================================================
-- MIGRATION: RO3 screenshot upload + host-only verification
-- ------------------------------------------------------------
-- Run this ONCE in the Supabase SQL editor, after schema.sql.
-- Purely additive — does not drop or rewrite any existing table,
-- safe to run even if some matches already exist from testing.
-- ============================================================

-- 1. New table: one row per round (screenshot), not per match.
--    The old schema's `matches.scoreboard_image_url` (single image)
--    stays in place untouched for backward compat / manual reference,
--    but match.py should stop writing to it and use this table instead.
create table if not exists match_screenshots (
    id              bigserial primary key,
    match_id        bigint not null references matches(id) on delete cascade,
    round_number    integer not null check (round_number in (1, 2, 3)),
    image_url       text not null,
    uploaded_by     bigint not null references players(id),
    raw_extraction  jsonb,                    -- per-round Vision/OCR output
    ocr_confidence  numeric(4,3),             -- 0.000-1.000, null if provider doesn't report one
    created_at      timestamptz not null default now(),

    unique (match_id, round_number)           -- one screenshot per round, re-upload = update not insert
);

create index if not exists idx_match_screenshots_match on match_screenshots(match_id);

-- 2. Track who uploaded (must equal matches.room_code_shared_by — enforce
--    this in app code in match.py, not here, since Postgres can't easily
--    check equality against another table's column in a CHECK constraint).

-- 3. New match status: screenshots are in, waiting on host's Approve click
--    in #result-verification (distinct from the existing 'awaiting_result',
--    which now means "waiting for all 3 screenshots to be uploaded").
alter table matches drop constraint if exists matches_status_check;
alter table matches add constraint matches_status_check check (
    status in (
        'forming',
        'map_vote',
        'awaiting_room',
        'in_progress',
        'awaiting_result',       -- room code shared, waiting on 1-3 screenshots
        'pending_verification',  -- all 3 screenshots uploaded + OCR'd, waiting on host approval
        'awaiting_review',       -- flagged for admin review (OCR low-confidence or stat outlier)
        'completed',
        'cancelled',
        'abandoned'  -- written by admin-scrap-match (AFK confirmation); was missing from this
                     -- migration originally, which blocked the constraint from applying at all
                     -- once any real abandoned-match rows existed. Found via live testing 2026-07-17.
    )
);

-- 4. Who approved the result in #result-verification, and when.
alter table matches add column if not exists approved_by bigint references players(id);
alter table matches add column if not exists approved_at timestamptz;

-- 5. IMPORTANT — decide before building the aggregation logic:
--    RO3 = 3 rounds/games played as one match. The workflow doc does not
--    say whether final per-player stats are (a) summed across all 3
--    rounds, (b) averaged, or (c) taken only from rounds the team won.
--    Recommended default, unless the team wants something else:
--      - kills / deaths / assists / damage / hill_time  -> SUM across 3 rounds
--      - impact / score                                  -> AVERAGE across 3 rounds
--      - match winner                                    -> whichever team won 2 of 3 rounds
--      - MVP                                              -> highest summed impact on the winning team
--    Whoever writes the finalize() rewrite in match.py should implement
--    exactly this (or the team's actual decision) reading from
--    match_screenshots instead of a single raw_extraction blob.