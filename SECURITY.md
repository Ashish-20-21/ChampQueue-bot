# Security Notes

## What "someone using the bot" can and can't do

Discord users interacting with the bot **cannot** extract your API keys,
tokens, or database credentials through any command — none of them are ever
included in a response, embed, or error message. The attack surface for
regular users is limited to what the commands themselves let them *do*, not
what secrets they can *see*. That surface is now:

| Risk | Status |
|---|---|
| Submitting a fake result for a match you're not in | **Fixed** — `/match-submit` and `/match-roomcode` now check the caller is one of that match's 10 players |
| Burning your Vision AI budget with junk/huge uploads | **Fixed** — 8MB cap + image-type check before anything is sent to the API (`config.MAX_SCOREBOARD_UPLOAD_BYTES`) |
| Spamming `/register` or `/queue-join` | **Fixed** — per-user cooldowns (`config.REGISTER_COOLDOWN_SECONDS`, `config.QUEUE_JOIN_COOLDOWN_SECONDS`) |
| A compromised admin account abusing `/admin-*` commands | **Not fixable in code** — see below |
| The Supabase service key or bot token leaking | **Not a code problem — an ops/secrets-hygiene problem** — see below |

## Things no amount of bot code can fix — these are on you operationally

1. **Guard the admin Discord role tightly.** Every `/admin-*` command
   (`admin-approve-match`, `admin-adjust-reputation`, `admin-correct-stat`,
   etc.) trusts anyone holding `ADMIN_ROLE_ID`. If that role gets handed out
   loosely, or an admin's Discord account gets phished, they can forge match
   outcomes and reputation values. Treat that role like a production
   credential — small trusted group, 2FA enforced Discord-side.

2. **Never commit `.env`.** It's already gitignored by convention in this
   layout, but double check before your first `git init` / `git push`.
   If a key ever does leak (accidental commit, screenshot, leaked host):
   - Discord bot token → regenerate in the Developer Portal immediately
     (old token is invalidated instantly).
   - Supabase service key → roll it in Supabase → Settings → API. This key
     bypasses Row Level Security entirely, so a leak means full DB read/write
     until rotated — treat it as your highest-value secret.
   - Anthropic/OpenAI/Qwen key → rotate in that provider's console; also
     check your usage dashboard for anomalous spend as a leak indicator.

3. **Consider enabling Supabase RLS as defense-in-depth**, even though the
   bot uses the service key to bypass it. That doesn't protect against the
   service key leaking, but it does mean if you ever add a second, lower-
   privilege client (e.g. a future web dashboard using the anon key), it
   fails closed instead of open by default.

4. **Run the bot on infrastructure you control and patch.** If the host
   running `bot.py` is compromised, the `.env` file on disk is compromised —
   that's true of literally any bot/backend, not specific to this one.
   Standard hygiene applies: keep the host patched, don't expose it beyond
   what's needed, use a secrets manager instead of a plain `.env` file if
   you're deploying somewhere that supports one (Railway, Fly.io, Render,
   AWS Secrets Manager, etc. all have first-class support for this).

5. **Rate limits are per-command, not global.** The cooldowns added here stop
   single-user spam on `/register` and `/queue-join` but don't protect
   against a coordinated multi-account attack. If that becomes a real
   concern, Discord's own AutoMod / server-level rate limiting on member
   actions is the right layer to add it at, not the bot.

## Bottom line

The keys were never reachable through the bot's own interface — that part
was safe by construction. The gaps that *did* exist were authorization gaps
(who's allowed to submit/roomcode a given match) and cost/spam-abuse gaps,
both of which are patched in this version. What's left is standard
credential and access hygiene that applies to literally any bot with a
database and API keys behind it, not something unique to this codebase.
