# Changelog

Each entry lists the exact file and function/section touched, so you can
jump straight to the diff instead of re-reading whole files.

---

## Round 2 — Server-hijack guard (guild lock)

- **`bot.py`**
  - Added `on_guild_join()` event handler — if the bot is added to any
    guild whose ID doesn't match `config.GUILD_ID`, it posts a one-time
    warning to the guild's system channel (if permitted) and immediately
    leaves.
  - Added a startup sweep inside `on_ready()` — iterates `self.guilds` and
    leaves anything that isn't the authorized guild, in case the bot was
    added to a foreign server while offline.

## Round 1 — Security hardening (authorization + abuse guards)

- **`cogs/match.py`**
  - Added `Match._is_match_participant()` static helper.
  - `match_roomcode()` — now rejects the caller unless they're one of the
    match's 10 players.
  - `match_submit()` — now rejects the caller unless they're one of the
    match's 10 players; added image-type check and
    `config.MAX_SCOREBOARD_UPLOAD_BYTES` size cap before the file is sent
    to the Vision AI provider.

- **`config.py`**
  - Added `MAX_SCOREBOARD_UPLOAD_BYTES`, `REGISTER_COOLDOWN_SECONDS`,
    `QUEUE_JOIN_COOLDOWN_SECONDS` under a new "Abuse / cost-control guards"
    section.

- **`cogs/registration.py`**
  - Added `import config`.
  - `register()` — added `@app_commands.checks.cooldown(...)` decorator.
  - Added `register_error()` handler for the cooldown's `CommandOnCooldown`
    exception, giving the user a friendly retry-after message instead of a
    raw error.

- **`cogs/queue.py`**
  - `queue_join()` — added `@app_commands.checks.cooldown(...)` decorator.
  - Added `queue_join_error()` handler, same pattern as above.

- **New file: `SECURITY.md`** — full writeup of what's fixed in code vs.
  what's an operational responsibility (admin role hygiene, key rotation
  steps, RLS, host hardening).

---

## Round 0 — Initial build

Everything else in the zip (schema, all cogs, all services, bot.py,
README.md) — first full implementation, no prior version to diff against.
