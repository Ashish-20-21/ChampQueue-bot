# 📜 Changelog

What changed in Champion's Queue, newest first. The project doesn't cut tagged releases, so entries are grouped by **dated milestone** and follow the [Keep a Changelog](https://keepachangelog.com) headings loosely.

---

## Unreleased · Impact-crown bonus

### Changed
- The **+5 MMR bonus now goes to the Impact crown holder** on each team (any row 1–5) instead of the yellow MVP tag, which is always row 1 and only reflects K/D. The vision prompt reads `has_crown`; a legacy `is_mvp`-only extraction is refused, never guessed.
- Verification card marks the crown rows with `👑 +5` and adds an `Impact` summary line (winner / loser crown positions).
- A crown on a player whose Impact is lower than a teammate's goes to review as a likely misread. Ties on Impact are fine; the crown breaks them.
- No database change: `is_mvp` keeps meaning "received the +5". Older matches keep their MVP-tag bonus.

---

## 2026-09-21 → 2026-10-01 · Performance and measurement

### Added
- **Discord call meter:** per-minute counts by call type and outcome, with per-second peaks and rate-limit detail.
- **Interaction timer:** one log line per click or command with a database, lock and Discord time breakdown.
- **`+result` text upload** alongside `/match-submit`, both feeding one shared pipeline, with optional screenshot forwarding.
- **India/ME-only queue** for players registered in that region. The Japan queue was deactivated, with its history kept.
- **Feature-switch registry** for voice channels, skill-vote storage, upload paths and timing.
- **Host tools:** `/host-replace-player` and `/admin-update-host`, plus a match status channel.
- **First automated test suite:** 144 tests.

### Changed
- The operator-skill vote answers in **one Discord call** from an in-memory roster.
- Career stats are recomputed with **one bulk call** per submission instead of ten.
- Match-start database writes are batched and no-op queue replies are removed.
- Match channels are deleted **15 minutes** after approval.

### Fixed
- **Discord rate-limit storms:** 193 hits in four minutes at the worst point, down to near zero afterwards.
- A losing AFK leaver could inherit a winning result.
- Leaderboard buttons could miss Discord's response window, and page numbers could run past the last page.

---

## 2026-09-02 → 2026-09-19 · Season 2

### Added
- **Season 2** cutover, Hall of Fame, Season Recap and a Season 1 participation badge.
- **Season Points:** +5 per win, −3 per loss, never below zero, shown on the verification card.
- **Shields** in three tiers, a **prize pool** that unlocks at 2000 points, and a fixed season end date.
- Staff and host tools: `/admin-adjust-sp`, `/host-roll-map`, `/report`, a Moderator role and a global command error handler.

### Changed
- Season 2 MMR reset.
- Maps no longer repeat back-to-back in the same queue.
- Per-match voice channels became optional (off by default).
- OCR bracket misreads and AFK leavers are now handled separately from name mismatches.

### Fixed
- A crash that broke every shield confirmation.
- Network-fault handling in the approval and cleanup sweeps.
- Season stats read from the wrong source.

---

## 2026-08-12 → 2026-08-31 · One-round matches and fairness

### Changed
- **Matches went from three rounds and three screenshots to one round and one screenshot (RO1)**, with one map instead of three.
- Teams are balanced on a composite rating with an uncertainty discount for newer players (random while players are new), replacing join order.

### Added
- **AFK / leaver detection** from the scoreboard.
- Staff toolkit: reset match, match card, manual result entry, queue clean and replace, map change.
- Achievements and a rebuilt `/player-stats`.
- **IGN confirmation flow** for names the OCR could not match.
- A central **incident log** channel.
- A rank-progress ladder and self-service IGN changes (2 per week).

### Fixed
- Round data is written **atomically**, so a network drop mid-write can no longer leave partial data.
- Match ID collisions, names truncated with "(...)" on the scoreboard, and Join/Leave retry on network faults.

---

## 2026-07-29 → 2026-08-09 · Global pivot

### Changed
- East and West merged into **one global pool** with four queues. Region became a label.
- The rank ladder was rebuilt into 200-point bands (Elite1 to Titans) with a one-time MMR reset.
- The leaderboard shows the full roster.

### Added
- Provisional stats, map-name translation for non-English game clients (9 languages), a redesigned Teams Formed embed and a publicly visible host-approve confirmation.

### Fixed
- Stale buttons after a restart and a skill-vote race on the fifth vote.

---

## 2026-07-09 → 2026-07-27 · First release

Live on **2026-07-20**.

### Added
- Registration with a 19-digit UID and instant approval.
- Queue, 10-player Start Match flow and match formation.
- Vision-based scoreboard reading and the MMR engine.
- Leaderboard, career stats and rank tiers.
- AFK reporting, a correction and review system, a 5-minute auto-approve and retry protection.

### Removed
- Captains and map voting, to keep matches simple.
