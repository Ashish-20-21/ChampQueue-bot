-- ============================================================
-- MIGRATION 005: correction-request system + auto-approve timeout
-- ------------------------------------------------------------
-- Two additions, designed to share one mechanism rather than two:
--
-- 1. matches.approval_deadline — set once, when the verification card
--    posts. A periodic sweep (same pattern as the existing cleanup_at
--    sweep) auto-approves any match past this deadline, UNLESS it has
--    an open match_issues row (see below) — the exact same guard the
--    manual Approve button uses, so there's no separate "pause the
--    timer" logic needed. One shared check, used twice.
--
-- 2. match_issues — one table for both /correction-result (host-filed,
--    pre-approval, blocks Approve/auto-approve until resolved) and
--    anything routed to review from a failed OCR/validation submission.
--    Two different channel posts (intake vs. resolved-log) can both
--    reference the same rows; no need for two separate schemas.
-- ============================================================

alter table matches add column if not exists approval_deadline timestamptz;

-- Re-affirm the full current status list (idempotent — matches what's
-- already live, just keeping this migration file authoritative going
-- forward instead of drifting from ad-hoc SQL run directly in the editor).
alter table matches drop constraint if exists matches_status_check;
alter table matches add constraint matches_status_check check (
    status in (
        'forming',
        'map_vote',
        'awaiting_room',
        'in_progress',
        'awaiting_result',
        'pending_verification',
        'awaiting_review',
        'completed',
        'cancelled',
        'abandoned'
    )
);

create table if not exists match_issues (
    id              bigserial primary key,
    match_id        bigint not null references matches(id),
    round_number    int check (round_number between 1 and 3),  -- null = whole-match issue, not round-specific
    reported_by     bigint not null references players(id),
    reason          text not null check (reason in (
                        'stat_correction',   -- a number is wrong
                        'result_issue',      -- broader "something's off" (map, score, roster)
                        'vision_failure',    -- OCR/extraction failed outright, routed here automatically
                        'approved_by_mistake' -- host filed after already approving (see match.py Case B)
                    )),
    detail_text     text,
    status          text not null default 'open' check (status in ('open', 'resolved')),
    resolved_by     bigint references players(id),
    resolved_at     timestamptz,
    resolution_note text,
    created_at      timestamptz not null default now()
);

create index if not exists idx_match_issues_match on match_issues(match_id);
-- Partial index: the only query the hot paths actually run is "is there an
-- open issue for this match" — this keeps that check cheap regardless of
-- how many resolved historical issues accumulate over a season.
create index if not exists idx_match_issues_open on match_issues(match_id) where status = 'open';
