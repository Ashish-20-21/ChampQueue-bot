# Tech concepts, explained like you're new (because we were)

Every term below showed up while we built and ran a Discord matchmaking bot.
Each one comes with **a picture to remember it**, **the grown-up name to Google**, and **one line on where we met it**.

`✅ built` we made it · `🗺️ planned` designed, not built yet · `👀 ran into it` we learned it the hard way

| Story | You'll meet |
|---|---|
| 🔐 Who may press which button? | RBAC · least privilege · break-glass · permission vs rule · secrets · private data |
| 📦 Keep your logs, don't overpay | incremental sync · retention · control vs data plane · pay-per-scan |
| 🛟 When things go wrong | 429 & backoff · idempotency · partial failure · ack-first · scheduled jobs |
| 🧪 Change things safely | config & defaults · mutation testing · golden-master tests · decision records |

---

## 🔐 Story 1 — Who may press which button?

**RBAC — Role-Based Access Control** `🗺️ planned`
Think of **hotel key cards**: the card opens the doors for its job (cleaner, manager), and when someone's job changes you change the card, not the doors.
In our bot: staff commands were fixed to a few tiers in the code. We designed a simple table, *role → allowed actions*, so a trial moderator can get three actions without touching the code.
*Also called:* permission matrix, ACL. *The rival idea:* ABAC (decide by attributes, not roles).

**Least privilege + fail closed** `✅ built` `🗺️ planned`
Give a house guest the key to the guest room, not the whole house. If a door has no rule written for it, **it stays locked**.
In our bot: the token for reading logs got the one permission it needed, even though the form offered dozens. The planned table treats anything unlisted as admin-only.
*Also called:* principle of least privilege, deny by default, fail-safe defaults.

**Break-glass access** `🗺️ planned`
The red box on the wall: *"in emergency, break glass"*. It's there for the one trusted person when normal things break, and the alarm records that it was used.
In our bot: one owner identity outside the normal roles that can bypass permission checks, with every use logged.
*Also called:* emergency access, superuser/root, privileged access management (PAM), audit trail, MFA.

**Permission vs rule (invariant)** `👀 ran into it`
A **VIP pass** gets you past the bouncer, but it doesn't make room in a club that's full. *Who may do it* (authorization) and *is it valid* (a business rule) are different questions.
In our bot: a rule stops a player being in two live matches at once. It's about the player being added, so even an owner needs a deliberate, named override.
*Also called:* authorization vs validation, invariants, audited override.

**Secrets management** `✅ built`
Don't tape your house key to the front door. Keep keys in a safe, give each one a narrow use and an expiry date, never read them out loud, and check the old photos before you post them.
In our bot: the script's token lives in encrypted CI secrets and shows as `***` in logs. A check explains a badly pasted secret by its length and shape, never its content. The whole repo history was scanned for key patterns before publishing.
*Also called:* secret scanning, token scopes, rotation, log masking.

**Keep private things private** `✅ built`
Don't pin private letters on a public noticeboard. Decide what's sensitive (user IDs, IP addresses, money, deal terms), store it privately, and keep only what you need.
In our bot: archived logs go to a private repo; the public journal leaves sensitive details out, and a local-only file holds them.
*Also called:* data classification, PII, data minimization.

---

## 📦 Story 2 — Keep your logs, don't overpay

**Pull-based incremental sync** `✅ built`
You visit the library every hour and ask only for **issues newer than your bookmark**. The bookmark is a **checkpoint** (also *watermark* or *cursor*). Three things can go wrong, and each has a name:
- *the same issue shows up twice* → drop duplicates by unique id
- *the newest issue isn't on the shelf yet* → skip the last couple of minutes (late-arriving data)
- *you were away too long and the library threw old issues out* → a **backfill** can only reach as far as the library keeps them

The opposite is **push**: the newspaper delivered to your door (streaming, webhooks, log drains, CDC).
In our bot: an hourly script copies the latest logs from a hosted database into files, resumes from its bookmark, and reports a gap if it was off too long.

**Retention + log rotation (hot/cold)** `✅ built`
The fridge and the freezer. Recent notes stay on your desk where you can read them fast (**hot**); old ones get squashed into a box (**cold**, compressed). And the source may bin its own copies after a while, so copy first.
In our bot: free-tier logs lived about a day; we keep daily files and compress older days.
*Also called:* retention policy, archival, hot/warm/cold storage.

**Control plane vs data plane** `👀 ran into it`
A restaurant: the dining room and kitchen are the **data plane** (the real service). The manager's office with the CCTV and the books is the **control plane**. Checking the CCTV doesn't slow the kitchen.
In our bot: the bot uses the database's data API; the log script uses the provider's management API with a different key, so reading logs adds no load to the live database.

**Pay-per-scan** `👀 ran into it`
A data plan billed by how much you *download*, not how much you keep. Searching a whole month of logs "downloads" a month, even to find one line. So search a narrow time window.
In our bot: browsing logs over a wide range used a whole month's allowance in a day; small hourly slices avoid it.
*Also called:* bytes scanned, partition pruning, query cost, quotas and grace periods.

---

## 🛟 Story 3 — When things go wrong

**Rate limiting, HTTP 429 and backoff** `✅ built`
A shop assistant says *"one customer at a time, please."* Keep shouting and you're served slower. Wait, try again, and if still told no, **wait twice as long** (that's *exponential backoff*). Sometimes they say *"come back in 30 seconds"*, and that hint is the `Retry-After` header. The real limit is often lower than you assumed, so watch for it.
In our bot: the log script got a 429 after about ten quick calls. It now waits, doubles its pause after each 429, follows the server's hint and says what it's doing. (No random jitter yet; it's a single client.)

**Idempotency + safe retries** `✅ built`
Pressing the lift button twice doesn't call two lifts. That's **idempotent**: doing it twice equals doing it once. Reads are like that, so you can retry them freely. Paying a bill twice is not, so guard writes.
In our bot: a lookup command retries temporary database hiccups because it only reads; duplicate log rows are dropped by their id.

**Partial failure + compensating actions** `👀 ran into it`
You book a trip: the flight works, the hotel fails. If you walk away you're stuck with half a trip, so you must **cancel the flight** (a *compensating action*). In distributed systems this pattern is called a **saga**.
In our bot: a flow created a record first, then a later step failed. The players went back to the queue, but the record stayed "forming" forever and piled up. The planned fix: mark it abandoned when the flow fails.
*Also called:* orphaned records, transactional integrity.

**Acknowledge first, process after** `✅ built`
The waiter says *"I'll be right with you"* within seconds, then goes to the kitchen. Chat platforms demand a fast reply, so the bot says "got it" first, does the slow database work, then sends the answer.
*Also called:* deferred response, asynchronous processing.

**Scheduled jobs on throwaway machines** `✅ built`
A night-shift worker with **no memory** who leaves a note for the next shift (the checkpoint), and a *"do not disturb"* sign so two workers never do the same job at once (a **concurrency lock**). Scheduled runs can also start a few minutes late, so the job must be fine with that.
In our bot: an hourly scheduled workflow commits the new data plus its checkpoint to a repo, and never runs two copies together.
*Also called:* cron, ephemeral runners, stateless jobs, mutual exclusion.

---

## 🧪 Story 4 — Change things safely, remember why

**Config and safe defaults** `✅ built`
A **thermostat dial**: change the setting without rebuilding the house, and if the dial breaks, it falls back to a sensible temperature. A typo in a setting shouldn't crash the bot or lock everyone out.
In our bot: a hard-coded per-user limit became one setting with a default; bad values fall back to the default.
*Also called:* externalized configuration, twelve-factor app, feature flags.

**Mutation testing + test doubles** `✅ built`
Test your smoke alarm by **making a little smoke**. Mutation testing breaks your code on purpose; if no test fails, the tests have a hole. *Test doubles* (fakes) are crash-test dummies: they stand in for real services so tests run without a network or secrets.
In our bot: breaking a few lines on purpose showed one important check that no test covered.

**Golden-master (characterization) tests** `🗺️ planned`
**Photograph the room before the renovation.** Record exactly how it behaves today, then prove the rebuild changed nothing before you change anything on purpose.
In our bot: planned for the permissions redesign, with a before/after comparison of who can do what.
*Also called:* approval testing, safe refactoring.

**Decision records, changelogs, incident notes** `✅ built`
A **captain's log** and hospital handover notes. Write down what we decided and why (*ADR*), what shipped (*changelog*), what broke and what we learned (*blameless postmortem*), and where to pick up tomorrow (*handoff*), so the knowledge isn't only in one head.
In our bot: a monthly journal in the repo with the same sections every day.

---

## 🎯 Test yourself

<details><summary>1. A trial moderator joins and should get only three actions. Which idea fits?</summary>

RBAC: give the role a short list, no code change needed.
</details>

<details><summary>2. A server answers "429". What do you do?</summary>

Pause, follow any `Retry-After` hint, then retry with a longer wait each time (backoff).
</details>

<details><summary>3. Your job ran twice and copied the same rows. What protects you?</summary>

Idempotency: deduplicate by a unique id so doing it twice equals doing it once.
</details>

<details><summary>4. Why doesn't an owner's VIP pass bypass "no double-booking"?</summary>

Permission and rule are different questions. A rule needs its own explicit, logged override.
</details>

<details><summary>5. Your scheduled job runs on a fresh machine each time. Where does it remember its place?</summary>

In a checkpoint stored outside the job (a saved file or database), like a note left for the next shift.
</details>

<details><summary>6. You broke the code on purpose and no test failed. What did you find?</summary>

A gap in your tests. That's what mutation testing is for.
</details>

---

*Simplified on purpose. For real security or production decisions, read the official docs.*
**Adding an entry:** copy a block above, give it a picture, the real name, one generic "In our bot" line (no command names, config keys, ids or figures), and a status tag.
