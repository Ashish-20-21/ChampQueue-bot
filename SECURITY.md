# 🔐 Security

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use this repository's **Security** tab (private vulnerability reporting) if it is enabled, or contact the maintainer through their GitHub profile. Include what you found, how to reproduce it and what you think the impact is.

---

## Trust boundaries: who can do what

| Boundary | How it is enforced |
|---|---|
| **Submitting a result** | Only the match host or an admin. The upload must be an image under 8 MB, and a second in-flight submission is blocked. |
| **Setting the room code** | Host only. Admins cannot. |
| **Staff commands** | Gated by Discord roles. **Admin** has full power, **Moderator** gets a limited set (MMR adjustments are capped at 50), and **HOD** confirms paid shield grants. |
| **Paid shields** | A two-person rule: the person who initiates a grant cannot be the person who confirms it. |
| **Approving a match** | One database function commits MMR, rank and points together and refuses unless the match is pending verification, so a repeat approval cannot score twice. |
| **Manual adjustments** | MMR and Season Points adjustments write an audit row recording who made the change and why. Reputation changes are logged with a reason. |
| **Where the bot runs** | It leaves any Discord server that is not its configured home server. |
| **Spam and cost abuse** | Per-user cooldowns on clicks, reports, IGN changes and leaderboard reloads, plus the upload size and type checks above. |

---

## Secrets

- Every credential is read from **environment variables** (`config.py`). `.env` is never committed, and `.env.example` contains placeholders only.
- As of **2026-10-02** a scan of the working tree and the full git history on every branch found no key- or token-shaped strings and no committed `.env`.
- The Supabase **`service_role` key bypasses row-level security**, so a leak means full read and write access until it is rotated. Treat it as the highest-value secret.

**If a key leaks:**

| Secret | Action |
|---|---|
| Discord bot token | Regenerate it in the Developer Portal. The old token is invalid immediately. |
| Supabase service key | Roll it in Supabase → Settings → API. |
| Vision provider key | Rotate it in the provider's console and check its usage dashboard for unusual spend. |

---

## What code cannot fix

1. **A compromised admin account.** Every staff command trusts the Discord role. Keep the admin role to a small, trusted group with 2FA enforced on Discord.
2. **A compromised host.** Whoever controls the machine running the bot controls its environment. Keep the host patched and use the host's secret storage.
3. **Coordinated multi-account abuse.** Per-user cooldowns stop one person spamming, not many accounts together. Server-level AutoMod and verification are the right layer for that.
