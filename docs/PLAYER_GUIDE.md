# 🎮 Player Guide

Everything a player needs, from first registration to Season 2 prizes.

> **In 30 seconds**
> 1. `/register` once with your 19-digit COD UID.
> 2. Join a queue. At **10 players** the bot builds two teams (balanced once everyone has 10+ matches, random before that) and opens a private match channel.
> 3. The **host** (whoever pressed *Start Match*) shares the room code, then uploads **one scoreboard screenshot** after the game.
> 4. The bot reads the screenshot. The host approves, or it auto-approves after **5 minutes**.
> 5. Your **MMR, rank and Season Points** update together.

---

## 1. Register (once)

Run `/register` with:

| Field | Notes |
|---|---|
| **COD UID** | 19 digits, exactly as shown in-game. Your permanent identity. |
| **IGN** | Your current in-game name. |
| **Region** | EU/AF, NA/Latam, India/ME or Japan. |
| **Organization** | Optional. |

A valid UID is approved instantly. A wrong UID saves nothing, so just run `/register` again. `/whoami` shows your status.

**Renamed in-game?** Use `/ign-change` (limited per week; the current limit is set by the admins). Your stats follow your UID, so a rename loses nothing.

---

## 2. Join a queue

| Queue | Who can join |
|---|---|
| EU/AF | Any registered player |
| NA/Latam | Any registered player |
| India/ME | Any registered player |
| India/ME-only | Players registered with the **India/ME** region |

Every queue feeds **one global pool**: one MMR, one rank ladder, one leaderboard. Tap **Join Queue** on the panel in a queue channel. Repeated taps are ignored for a few seconds, and Join switches off when the queue is full (10/10).

---

## 3. Your match

1. **Start Match.** At 10/10 a *Start Match* button appears. Whoever presses it becomes the **host**. The queue then resets for the next group.
2. **Teams and map.** Players are split into two teams of 5 (Defender and Attacker). When all 10 players have completed at least **10 matches**, the split is balanced using MMR plus win rate, K/D, hill time and MVP rate. If anyone is newer than that, the teams are **random**. One map is picked at random from Summit, Hacienda, Combine, Takeoff and Arsenal, and never the same map as the previous match in that queue.
3. **Private match channel.** The bot opens a channel for the 10 players with the teams, the map and two operator-skill panels.
4. **Operator-skill vote.** Tap one skill on your own team's panel. First tap wins: once a skill is taken, teammates can't pick it. Buttons stop working 10 minutes after the last tap.
5. **Room code.** The host types `+rc<code>` (for example `+rc241905`) or uses `/rc`. To fix a typo: `+urc<code>`.
6. **Play** one Hardpoint round on the announced map.
7. **Submit the result.** The host uploads **one scoreboard screenshot** with `/match-submit`, or types `+result` with the image attached.
8. **Check the card.** The bot posts a verification card with both teams' stats and the proposed MMR and Season Points. Nothing is applied yet.
9. **Approve.** The host presses **Approve**, or the match **auto-approves after 5 minutes**. MMR, rank and points update together.
10. **Cleanup.** The match channel is deleted about 15 minutes later.

**Good to know**
- The bot trusts the **scoreboard**: teams and winner come from the screenshot, not from how the lobby was announced.
- If a player left mid-match, the bot adds them as a leaver with a **losing** result. Unclear cases go to staff review.
- If a name can't be matched, staff get confirm buttons. Anything else odd (wrong map, unreadable score, a tie) goes to **staff review**, and the host is told.

---

## 4. Ranks and MMR

New players start at **200 MMR (Elite1)**. Your change in a match depends on your **position on your team's scoreboard**, the **result**, and the **Impact crown** (+5). The crown is the small crown icon next to the Impact number on the scoreboard. One player per team holds it, on **any** row 1–5, so play the objective, not just kills. The yellow MVP tag no longer matters for MMR.

| Position | 1st | 2nd | 3rd | 4th | 5th |
|---|:---:|:---:|:---:|:---:|:---:|
| **Win** | +9 | +8 | +6 | +4 | +3 |
| **Loss** | −3 | −4 | −6 | −8 | −9 |

**MMR never goes below 0.** A loss at 0 stays at 0, and your next win counts from there.

| MMR | 0 | 201 | 401 | 601 | 801 | 1001 | 1201 | 1401 | 1601 | 1801 | 2001+ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Rank** | Elite1 | Elite2 | PRO1 | PRO2 | Master1 | Master2 | Grandmaster1 | Grandmaster2 | Legendary1 | Legendary2 | Titans |

---

## 5. Season 2

**Season Points (SP)** are separate from MMR and decide the prizes.

- Each approved match gives **+5 for a win** and **−3 for a loss**.
- SP **never goes below 0**. Like MMR, a loss at 0 stays at 0 and nothing is owed.
- **Shields** change how a match scores for a limited time. You can hold one at a time.

| Shield | Duration | Win | Loss | How to get it |
|---|---|---|---|---|
| **Normal** | 72 h | +10 | 0 | 150 credits on the shield panel, or a ₹30 Boost |
| **2x Normal** | 144 h | +10 | 0 | ₹50 Boost |
| **Premium** | 72 h | **+50 for the first 24 h**, then +10 | 0 | ₹60 Boost |

Boost (paid) shields are granted by staff and confirmed by a second person before they activate.

**Prize pool**
- If anyone reaches **2000 SP**, the pool unlocks for the season. At the end, ranks **1 / 2 / 3** receive **₹700 / ₹500 / ₹300**.
- If nobody does, the top 3 each receive **their SP ÷ 5, in ₹**.
- **Season 2 ends 10 October 2026, 23:59:59 IST.** Whoever holds 1st, 2nd and 3rd at that moment takes the prize positions.

The points leaderboard panel has a **Reload** button.

---

## 6. If something goes wrong

| Problem | What to do |
|---|---|
| Wrong or bad map | The host uses `/host-roll-map` before uploading the result. |
| A player is missing | The host swaps in a sub with `/host-replace-player` (2 per match). Staff can always replace players. |
| Someone went AFK | Anyone can run `/afk @player`. Staff decide what happens next. |
| Rule-breaking | `/report @player` with a reason (5 per hour). Staff review it. |
| Result looks wrong | The host runs `/correction-result`. While a correction is open, the match can't be approved. |
| Host went quiet | Auto-approve covers it after 5 minutes. Staff can also move the host role. |

After approval nothing is reversed automatically. Staff fix mistakes by adjusting MMR or points directly.

---

## 7. Command cheat sheet

| Command | Who | Purpose |
|---|---|---|
| `/register`, `/whoami` | Everyone | Sign up, check your status |
| `/ign-change` | Everyone (weekly limit) | Update your in-game name |
| `/queue-status` | Everyone | See who is in a queue |
| `/player-stats`, `/cs-stats` | Everyone | Career and current-season stats |
| `/rank-progress`, `/achievements` | Everyone | Progress to the next rank, badges |
| `/afk @player`, `/report @player` | Everyone | Report a problem player |
| `+rc<code>`, `+urc<code>`, `/rc` | Host | Set or fix the room code |
| `/host-roll-map`, `/host-replace-player` | Host | Fix a map or swap in a sub |
| `/match-submit` or `+result` + image | Host | Upload the scoreboard |
| `/correction-result` | Host | Flag a problem with a result |
