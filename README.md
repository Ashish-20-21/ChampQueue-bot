# Champion's Queue — Discord Bot

Invite-only competitive matchmaking bot for the COD Mobile esports community.
Python (discord.py) + Supabase (Postgres) + swappable Vision AI extraction.

> **Design decisions and their reasoning live in [`DECISIONS.md`](./DECISIONS.md),
> not in this file.** This README describes what the bot does and how to run
> it. If you're wondering *why* something works a certain way, check
> `DECISIONS.md` first — it's the source of truth and gets updated whenever a
> real decision changes. This file can drift; that one shouldn't.

---

## 1. Setup

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env with your real values (see below)
```

### Required `.env` values
| Variable | Where to get it |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord Developer Portal → your application → Bot |
| `GUILD_ID` | Right-click your server → Copy Server ID (Developer Mode on) |
| `ADMIN_ROLE_ID` | Right-click the admin role → Copy Role ID |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project → Settings → API (use the **service_role** key, not anon — the bot needs to bypass RLS) |
| `ANTHROPIC_API_KEY` | console.anthropic.com (default vision provider) |
| `DIGEST_CHANNEL_ID` | optional — channel for the daily digest post |

### Discord bot permissions/intents
Enable **Server Members Intent** and **Message Content Intent** in the
Developer Portal. Invite the bot with at least: `Manage Channels`,
`Send Messages`, `Use Slash Commands`, `Connect` (voice channel creation).

### Database
Run these once, in order, in the Supabase SQL editor:
1. `database/schema.sql` — creates every table, seeds achievements, creates Season 1
2. `database/migration_ro3_verification.sql` — RO3 screenshot table + host-approval tracking
3. `database/migration_002_region_and_maps.sql` — map pool column + East/West region constraint

### Discord server setup (not code — you do this manually)
- Create one text channel per region for the queue panel (e.g. `#queue-east`,
  `#queue-west`). Channel visibility/permissions are your responsibility —
  the bot does not create or hide these channels.
- After the bot is running, post the persistent queue panel once per region:
  `/queue-post East` (run inside `#queue-east`), `/queue-post West` (inside
  `#queue-west`).

### Run it
```bash
python3 bot.py
```

---

## 2. Architecture

```
bot.py                  entry point, loads all cogs, syncs slash commands
config.py               tunable constants (MMR weights, reputation
                         thresholds, vote timeouts, map pool, region list, etc.)
database/
  schema.sql             run once in Supabase
  migration_ro3_verification.sql    RO3 screenshots + approval tracking
  migration_002_region_and_maps.sql  map_pool column + East/West constraint
  db.py                  ALL synchronous database access goes through this file
                         also exposes `adb` — an async-safe proxy (see below)
services/
  matchmaking.py         team balance ONLY — no captain selection (removed)
  mmr_engine.py           MMR delta formula + rank derivation
  vision_extraction.py   swappable Vision AI provider interface
  validation.py          outlier + vote-mismatch checks -> auto-accept or review
  reputation.py          penalty amounts + tiered consequences
  stats_engine.py        career-stat recompute + achievement checks, post-match
cogs/
  registration.py        /register /update-ign /whoami — region locked to a
                         dropdown (East/West only), validated server-side too
  queue.py                Region-scoped queue. `/queue-post` (admin-only,
                         posts the persistent Join/Leave panel per region),
                         `/queue-status`. No captains, no map vote — see
                         DECISIONS.md. Handles match formation: team split,
                         skill vote, map announcement, private channel/VC
                         creation, room-code parsing (`+room <code>` in the
                         match's private text channel).
  match.py                `/match-roomcode`, `/match-submit` (scoreboard
                         screenshot -> vision extraction -> winner vote ->
                         MMR finalize). Still uses sync `db`, not `adb` —
                         unmigrated. [in progress — see DECISIONS.md for
                         the open RO3→MMR aggregation question; the current
                         code computes one MMR change per match, not yet
                         per-round]
  stats.py                /profile /leaderboard /compare-last-match
                         /rank-progress /achievements — all region-scoped
  admin.py                 /admin-approve /admin-reject /admin-review-queue
                         /admin-approve-match /admin-correct-stat
                         /admin-adjust-reputation
  digest.py                daily automated summary post
utils/
  embeds.py               all Discord embed builders
  permissions.py          admin-role check decorator
```

### `db` vs `adb` — which to use
`database/db.py` exposes two things: `db` (synchronous — blocks the whole
bot while waiting on Supabase) and `adb` (async-safe wrapper — doesn't
block). **All new or edited code must use `adb`, always, no exceptions.**
See `DECISIONS.md` → "db vs adb" for the full reasoning. If you find a
`db.<method>(...)` call anywhere in a cog, that's leftover/unmigrated code,
not an intentional choice.

---

## 3. Confirmed design decisions

Full reasoning for each of these lives in [`DECISIONS.md`](./DECISIONS.md).
Summary only, here:

- **No captain system.** Every player on a team is identical. The only
  privileged player is whoever clicks "Start Match" — they're the Host for
  that match's entire lifecycle (room code + approval).
- **No map vote.** Backend picks 3 maps from the Hardpoint pool and
  announces them plainly. No player input on map selection.
- **RO3 ≠ Best-of-3.** All 3 rounds are always played and always count
  individually — there's no "stop early at 2 wins" logic. How the 3 rounds
  combine into a single MMR/result is **still open** — see DECISIONS.md.
- **Exactly two regions: "East" and "West".** Casing matters — it's
  enforced by a DB constraint and locked in the `/register` dropdown.
- **Skill-vote picks are stored but not currently shown in the match-log.**
  Flagged as NOT FINAL in DECISIONS.md — data is kept for future analysis,
  display was deliberately reduced from the original spec.

---

## 4. Still open / not yet built

- **RO3 → MMR aggregation formula** — blocking `match.py`'s finalize logic.
  See DECISIONS.md.
- **Vision AI provider** — `AnthropicVisionProvider` is implemented and
  working; `OpenAIVisionProvider` / `QwenVisionProvider` are stubs.
  `LayoutOCRProvider` (primary, OCR-based) is scaffolded but `.extract()`
  is not yet implemented.
- **IGN-to-player matching on submission** — currently exact string match
  against the registered roster; no fuzzy matching yet.
- **Fastest Climbers (daily digest)** — needs a `mmr_snapshots` table, not
  built yet.
- **Discord role automation** — explicitly deferred, not built.
- **Bot error/audit logging** — no `#bot-logs` channel wiring yet.

---

## 5. Testing notes

No automated test suite yet. Current verification is manual:
`python -m py_compile <file>` for syntax, then live local testing against a
private test Discord server before any change is considered done. See
`DECISIONS.md` for anything that changed behavior as a result of testing.