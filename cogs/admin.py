import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import db, adb, with_retry
from services import reputation, mmr_engine
from utils.embeds import verification_card, hall_of_fame_embed
from utils.permissions import admin_only, is_admin
from utils import incident_log
from cogs.queue import RegionQueueView, make_queue_embed

logger = logging.getLogger("champions_queue")

_ADMIN_SCORE_RE = re.compile(r"^(\d+)\s*[:\-]\s*(\d+)$")  # same pattern as cogs/match.py's _SCORE_RE


@app_commands.default_permissions(manage_guild=True)
class Admin(commands.Cog):
    """default_permissions above is a UI hint only (Discord's own docs:
    'members are NOT required to have the permissions given to actually
    execute this command') — it hides these commands from the slash-
    command picker for non-admins, but @admin_only() on each command
    below is what actually enforces access. Keep both; removing either
    weakens a different half of this."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # admin-approve / admin-reject — COMMENTED OUT (2026-08-15), not deleted.
    # Confirmed dead relative to the live registration flow: /register
    # (cogs/registration.py) auto-approves every successful registration
    # immediately via adb.approve_player(..., approved_by="auto") in the
    # same request — there is no manual admin-review step in the current
    # design (UID format check + in-server screenshot verification by
    # admins replaced the original "admin manually approves/rejects"
    # workflow from the earliest pre-launch version). A player row can
    # only ever sit at status='pending' if create_player() succeeded but
    # the immediate follow-up approve_player() call failed/never ran —
    # an edge case, not the designed path these two commands were built
    # for. Kept commented rather than deleted in case manual review is
    # reintroduced later (e.g. suspicious-registration flagging); db.py's
    # approve_player()/reject_player() methods are untouched.
    #
    # @app_commands.command(name="admin-approve", description="[Admin] Approve a pending player by their Discord user")
    # @admin_only()
    # async def approve(self, interaction: discord.Interaction, user: discord.Member):
    #     player = await adb.get_player_by_discord_id(user.id)
    #     if not player:
    #         await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
    #         return
    #     await adb.approve_player(player["id"], str(interaction.user.id))
    #     await interaction.response.send_message(f"Approved **{player['ign']}** ({user.mention}).", ephemeral=True)
    #
    # @app_commands.command(name="admin-reject", description="[Admin] Reject a pending registration")
    # @admin_only()
    # async def reject(self, interaction: discord.Interaction, user: discord.Member):
    #     player = await adb.get_player_by_discord_id(user.id)
    #     if not player:
    #         await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
    #         return
    #     await adb.reject_player(player["id"])
    #     await interaction.response.send_message(f"Rejected registration for **{player['ign']}**.", ephemeral=True)

    @app_commands.command(name="admin-review-queue", description="[Admin] List matches awaiting review")
    @admin_only()
    async def review_queue(self, interaction: discord.Interaction):
        res = await asyncio.to_thread(
            lambda: db.client.table("matches").select("*").eq("status", "awaiting_review").execute()
        )
        if not res.data:
            await interaction.response.send_message("No matches currently need review.", ephemeral=True)
            return
        lines = [f"`{m['match_id']}` — maps: {', '.join(m.get('map_pool') or []) or '—'} — created {m['created_at']}" for m in res.data]
        await interaction.response.send_message("**Matches awaiting review:**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="admin-correct-round", description="[Admin] Correct one player's position/MVP for a round")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)", round_number="Which round (always 1 for new RO1 matches; 1-3 kept for old RO3 matches)",
                            user="The player to correct", position="New position (1-5) — leave blank to keep current",
                            is_mvp="New MVP flag — leave blank to keep current")
    @admin_only()
    async def correct_round(self, interaction: discord.Interaction, match_id: str, round_number: app_commands.Range[int, 1, 3],
                             user: discord.Member, position: app_commands.Range[int, 1, 5] | None = None,
                             is_mvp: bool | None = None):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        if position is None and is_mvp is None:
            await interaction.response.send_message("Provide at least one of position or is_mvp to change.", ephemeral=True)
            return

        existing = [row for row in await adb.get_match_round_results(match["id"]) if row["round_number"] == round_number]
        target = next((row for row in existing if row["player_id"] == player["id"]), None)
        if not target:
            await interaction.response.send_message(
                f"No round {round_number} result exists yet for **{player['ign']}** on this match — "
                "the round needs to be submitted (even if flagged for review) before it can be corrected.",
                ephemeral=True,
            )
            return

        new_position = position if position is not None else target["position"]
        new_is_mvp = is_mvp if is_mvp is not None else target["is_mvp"]

        # Same guardrails _prepare_rounds already enforces at submission
        # time — reused here, not reimplemented, so an admin correction
        # can't quietly create the exact kind of invalid round the normal
        # upload path already refuses to accept.
        team = target["team"]
        others_same_team = [row for row in existing if row["team"] == team and row["player_id"] != player["id"]]
        if any(row["position"] == new_position for row in others_same_team):
            await interaction.response.send_message(
                f"Position {new_position} is already taken on team {team} for round {round_number}.", ephemeral=True
            )
            return
        if new_is_mvp and any(row["is_mvp"] for row in others_same_team):
            await interaction.response.send_message(
                f"Team {team} already has an MVP for round {round_number} — only one allowed.", ephemeral=True
            )
            return

        # Determine "won" from the round's actual recorded final_score —
        # the same source of truth _prepare_rounds uses at submission time.
        # NOT derived from the existing row's mmr_delta sign: that's
        # provably unsafe, e.g. a 1st-place MVP on the LOSING team scores
        # -3 (loss) + 5 (MVP) = +2, a positive delta despite losing —
        # inferring "won" from a positive sign there would be backwards.
        screenshot = await adb.get_match_screenshot(match["id"], round_number)
        score_text = str((screenshot or {}).get("raw_extraction", {}).get("final_score") or "")
        score_match = _ADMIN_SCORE_RE.fullmatch(score_text)
        if not score_match:
            await interaction.response.send_message(
                f"Round {round_number}'s stored final score ({score_text!r}) isn't readable — "
                "can't safely determine win/loss to recompute MMR. Fix the score first or handle this one manually.",
                ephemeral=True,
            )
            return
        winning_team = "A" if int(score_match.group(1)) > int(score_match.group(2)) else "B"
        won = team == winning_team
        new_delta = mmr_engine.calculate_mmr_change(new_position, won, new_is_mvp)

        await adb.correct_match_round_result(target["id"], new_position, new_is_mvp, new_delta)
        await interaction.response.send_message(
            f"Round {round_number}, **{player['ign']}**: position → {new_position}, MVP → {new_is_mvp}, "
            f"MMR delta → {new_delta:+d}. Not yet applied to their MMR — still needs approval.",
            ephemeral=True,
        )

    @app_commands.command(name="admin-force-approve", description="[Admin] Approve a match once all 10 round-result rows exist")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)")
    @admin_only()
    async def force_approve(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match["status"] not in ("awaiting_review", "pending_verification"):
            await interaction.response.send_message("This match isn't in a state that needs force-approval.", ephemeral=True)
            return

        match_cog = self.bot.get_cog("Match")
        if not match_cog:
            await interaction.response.send_message("Match cog isn't loaded — can't approve.", ephemeral=True)
            return
        admin_player = await adb.get_player_by_discord_id(interaction.user.id)
        await interaction.response.defer(thinking=True)
        success, message = await match_cog._do_approve(interaction.guild, match["id"], admin_player["id"] if admin_player else None)
        if not success:
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.followup.send(f"Match **{match_id}** force-approved by admin.", ephemeral=True)


    @app_commands.command(name="admin-adjust-reputation", description="[Admin] Manually adjust a player's reputation")
    @admin_only()
    async def adjust_reputation(self, interaction: discord.Interaction, user: discord.Member, delta: int, reason: str):
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        updated = await adb.apply_reputation_delta(player["id"], delta, f"admin_adjustment: {reason}")
        await interaction.response.send_message(
            f"**{player['ign']}** reputation now **{updated['reputation']}** ({'+' if delta >= 0 else ''}{delta}, reason: {reason})",
            ephemeral=True,
        )

    @app_commands.command(name="admin-adjust-mmr", description="[Admin] Manually adjust a player's MMR (disciplinary — e.g. after repeated AFK warnings)")
    @admin_only()
    async def adjust_mmr(self, interaction: discord.Interaction, user: discord.Member, delta: int, reason: str):
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message("Player not found.", ephemeral=True)
            return
        updated = await adb.apply_mmr_adjustment(player["id"], delta, reason, str(interaction.user.id))
        await interaction.response.send_message(
            f"**{player['ign']}** MMR now **{updated['mmr']}** ({'+' if delta >= 0 else ''}{delta}, reason: {reason}) "
            f"— logged, run by {interaction.user.mention}.",
            ephemeral=True,
        )

    # /admin-ign-change — COMMENTED OUT (2026-08-15). Replaced by
    # /ign-change below which is shared between admins and players
    # (admins unlimited, players rate-limited to 2/week). The old
    # "no history table, in-channel message IS the audit trail"
    # approach (2026-08-08) is superseded by ign_change_history table
    # now that players can self-serve and won't always be in the admin
    # channel. db.py's update_ign() is still used by the new command.
    #
    # @app_commands.command(name="admin-ign-change", description="[Admin] Change a player's IGN")
    # @admin_only()
    # async def admin_ign_change(self, interaction: discord.Interaction, user: discord.Member, new_ign: str):
    #     ...  (see git history for full body)

    @app_commands.command(name="admin-dispatch", description="[Admin] Trigger a season-related broadcast (Hall of Fame, weekly digest, etc.)")
    @app_commands.describe(
        category="Which broadcast to run",
        season_id="Optional — run for a specific season instead of whichever is currently active (e.g. re-running Season 1's Hall of Fame after Season 2 started)",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="Hall of Fame", value="hall_of_fame"),
        # Add future categories here (weekly digest, etc.) as new Choice
        # entries + a new _CATEGORY_HANDLERS entry below. This is a single
        # Discord choice parameter, not shared-message buttons — deliberate,
        # see the 2026-08 skill-vote 5th-voter race condition writeup in
        # DEV_NOTES for why multi-handler commands here never use buttons.
        # Discord's own picker means exactly one branch runs per invocation,
        # no shared message state to race on.
    ])
    @admin_only()
    async def dispatch(self, interaction: discord.Interaction, category: app_commands.Choice[str], season_id: int | None = None):
        await interaction.response.defer(ephemeral=True)
        handler = _CATEGORY_HANDLERS.get(category.value)
        if handler is None:
            await interaction.followup.send(f"No handler wired for `{category.value}` yet.", ephemeral=True)
            return
        try:
            # season_id is passed positionally as an override; handlers
            # that aren't season-scoped (a future weekly-digest, etc.)
            # simply don't declare the param and this is a no-op for them.
            result_message = await handler(self, interaction, season_id=season_id)
        except Exception as exc:
            logger.exception("admin-dispatch handler failed for category=%s", category.value)
            await incident_log.post(
                self.bot, f"admin-dispatch `{category.value}` failed: {exc!r}", level="error",
            )
            await interaction.followup.send(f"❌ `{category.value}` failed — see #botlog for details.", ephemeral=True)
            return
        await interaction.followup.send(result_message, ephemeral=True)

    async def _dispatch_hall_of_fame(self, interaction: discord.Interaction, season_id: int | None = None) -> str:
        """Fetches the target season, pulls the winner for all 9 HOF
        categories (each query already has its own >=8-match floor or
        explicit no-floor decision — see migration_024's header comment),
        writes each winner to hall_of_fame (upsert on season_id+category,
        safe to re-run if something needs correcting), then posts the
        embed to HALL_OF_FAME_CHANNEL_ID. Missing channel config fails
        loudly rather than silently no-op'ing, same lesson as the
        BOTLOG_CHANNEL_ID gap.

        season_id: optional override from /admin-dispatch's parameter.
        Defaults to whatever's currently active — but Season 1 ending
        without HOF ever being posted, followed by Season 2 activating,
        is exactly why this exists: without an override, this would
        silently compute Season 2's (empty) stats instead once Season 2
        goes active. Passing season_id=1 explicitly re-targets Season 1
        regardless of what's active right now."""
        if season_id is not None:
            season = await adb.get_season_by_id(season_id)
            if not season:
                return f"❌ No season found with id={season_id}."
        else:
            season = await adb.get_active_season()
            if not season:
                return "❌ No active season found — run migration_023_season_activation.sql first, or pass season_id explicitly."

        season_id = season["id"]
        categories = {
            "most_consistent": adb.hof_most_consistent,
            "fastest_climber": adb.hof_fastest_climber,
            "highest_total_kills": adb.hof_highest_total_kills,
            "best_avg_kills": adb.hof_best_avg_kills,
            "best_avg_deaths": adb.hof_best_avg_deaths,
            "most_mvps": adb.hof_most_mvps,
            "most_matches_played": adb.hof_most_matches_played,
            "best_kd": adb.hof_best_kd,
        }

        winners: dict[str, dict | None] = {}
        for key, fn in categories.items():
            winners[key] = await with_retry(fn, season_id)
        # highest_mmr takes no season_id — current snapshot, not season-scoped,
        # same regardless of which season is being posted for.
        winners["highest_mmr"] = await with_retry(adb.hof_highest_mmr)


        # Persist each winner. value column is text — stringify whatever
        # this category's headline number is; skip categories nobody
        # qualified for rather than writing a garbage row.
        value_keys = {
            "most_consistent": "win_rate_pct", "fastest_climber": "mmr_per_match",
            "highest_total_kills": "total_kills", "best_avg_kills": "avg_kills",
            "best_avg_deaths": "avg_deaths", "most_mvps": "mvp_count",
            "most_matches_played": "matches_played", "best_kd": "kd_ratio",
            "highest_mmr": "mmr",
        }
        for category, row in winners.items():
            if row is None:
                continue
            await with_retry(
                adb.record_hall_of_fame, season_id, category, row["player_id"], str(row[value_keys[category]]),
            )

        if not config.HALL_OF_FAME_CHANNEL_ID:
            return "⚠️ Winners recorded to DB, but HALL_OF_FAME_CHANNEL_ID isn't set — nothing posted. Set it and re-run."

        channel = interaction.guild.get_channel(config.HALL_OF_FAME_CHANNEL_ID) if interaction.guild else None
        if channel is None:
            return f"⚠️ Winners recorded to DB, but channel {config.HALL_OF_FAME_CHANNEL_ID} wasn't found — check HALL_OF_FAME_CHANNEL_ID."

        embed = hall_of_fame_embed(season, winners)
        await channel.send(embed=embed)
        return f"✅ Hall of Fame posted to {channel.mention} and recorded for {season.get('code') or season.get('name')}."


    @app_commands.command(name="admin-scrap-match", description="[Admin] Confirm an AFK report and scrap the match — VCs deleted now, text channel after 1hr")
    @admin_only()
    async def scrap_match(self, interaction: discord.Interaction, match_id: str, reason: str):
        # Normalize case — match_id is always stored uppercase (CQ-XXXX) but
        # admins will naturally type whatever case they saw it in (channel
        # names are lowercase, match-log embeds show uppercase). Normalizing
        # here beats relying on everyone remembering the exact case.
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match["status"] in ("completed", "cancelled", "abandoned"):
            await interaction.response.send_message(f"Match is already `{match['status']}` — nothing to scrap.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # VCs die immediately — no reason to keep them around once a match
        # is confirmed dead, unlike the text channel's 1hr review window.
        for vc_field in ("voice_channel_a_id", "voice_channel_b_id"):
            vc_id = match.get(vc_field)
            if not vc_id:
                continue
            vc = guild.get_channel(int(vc_id)) if guild else None
            if vc:
                try:
                    await vc.delete(reason=f"Match scrapped: {reason}")
                except discord.HTTPException:
                    pass

        cleanup_at = (discord.utils.utcnow() + timedelta(seconds=config.MATCH_CHANNEL_CLEANUP_DELAY_SECONDS)).isoformat()
        await adb.mark_match_abandoned(match["id"], cleanup_at)

        text_channel_id = match.get("text_channel_id")
        text_channel = guild.get_channel(int(text_channel_id)) if guild and text_channel_id else None
        if text_channel:
            try:
                await text_channel.send(
                    f"⚠️ This match has been scrapped by an admin (`{reason}`). "
                    f"This channel will be deleted automatically in ~1 hour. "
                    f"Please return to the queue to start a new match."
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"Match `{match_id}` marked abandoned. VCs deleted, text channel will auto-delete in ~1hr.",
            ephemeral=True,
        )

    # ── /admin-reset-match (2026-08-15) ────────────────────────────
    @app_commands.command(name="admin-reset-match",
                          description="[Admin] Reset a failed submission so the host can re-upload screenshots")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)")
    @admin_only()
    async def reset_match(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        allowed_statuses = ("pending_verification", "awaiting_review", "awaiting_result")
        if match["status"] not in allowed_statuses:
            await interaction.response.send_message(
                f"Match is `{match['status']}` — can only reset matches in "
                f"{', '.join(f'`{s}`' for s in allowed_statuses)}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        old_status = match["status"]
        counts = await adb.reset_match_for_resubmission(match["id"])

        roster_count = counts["match_players_count"]
        had_data = (counts["results_deleted"] + counts["stats_deleted"] + counts["screenshots_deleted"]) > 0

        if roster_count != 10:
            await interaction.followup.send(
                f"⚠️ Match `{match_id}` reset to `awaiting_result` (was `{old_status}`), but `match_players` has "
                f"**{roster_count}** rows (expected 10). The host's re-upload will fail with "
                f"\"all IGNs unknown\" unless the roster is restored first — this is the exact "
                f"bug that broke CQ-8758 and CQ-1612. Needs a manual DB fix before re-upload.",
                ephemeral=True,
            )
            return

        if had_data:
            await interaction.followup.send(
                f"✅ Match `{match_id}` reset to `awaiting_result` (was `{old_status}`).\n"
                f"Cleared: {counts['screenshots_deleted']} screenshots, "
                f"{counts['stats_deleted']} stats, {counts['results_deleted']} round results, "
                f"{counts['issues_deleted']} issues.\n"
                f"Roster intact ({roster_count} players). Host can re-upload now.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Match `{match_id}` reset to `awaiting_result` (was `{old_status}`).\n"
                f"No result data was stored — the failure happened before any rows were written "
                f"(likely an IGN resolution failure during OCR).\n"
                f"Roster intact ({roster_count} players). Host can re-upload now.",
                ephemeral=True,
            )

    # ── /admin-match-card (2026-08-15) ───────────────────────────
    @app_commands.command(name="admin-match-card",
                          description="[Admin] View a match's verification card (read-only inspection)")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)")
    @admin_only()
    async def match_card(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        round_results = await adb.get_match_round_results(match["id"])
        roster = await adb.get_match_players(match["id"])
        stats = await adb.get_match_player_stats(match["id"])

        # No round results AND no roster — genuinely nothing to show
        if not round_results and not roster:
            await interaction.response.send_message(
                f"Match `{match_id}` — **Status:** `{match['status']}`\n"
                f"No roster or result data exists for this match.",
                ephemeral=True,
            )
            return

        # Get all player IDs we need to look up (from results + roster)
        all_player_ids = list(set(
            [r["player_id"] for r in round_results] +
            [mp["player_id"] for mp in roster]
        ))
        players_data = await adb.get_players_by_ids(all_player_ids)
        players_by_id = {p["id"]: p for p in players_data}

        # Build per-player extraction entries from whatever data exists.
        # Players with round_results get real data; roster players without
        # results get dash placeholders — shows "we know they're in the
        # match, but no stats were recorded" which is more informative
        # than silently omitting them.
        extraction_players = []
        results_player_ids = {r["player_id"] for r in round_results}

        for r in round_results:
            player = players_by_id.get(r["player_id"], {})
            stat = next((s for s in stats if s["player_id"] == r["player_id"]
                         and s.get("round_number", 1) == r["round_number"]), {})
            extraction_players.append({
                "ign": player.get("ign", "?"),
                "team": r["team"],
                "position": r["position"],
                "is_mvp": r["is_mvp"],
                # None (not "—") for missing stats — verification_card's
                # display layer converts None to "—" for rendering; this
                # keeps the data itself consistently typed (int or None,
                # never a mixed str/int) so any future sort/math on these
                # fields doesn't repeat the 2026-08-19 TypeError crash.
                "kills": stat.get("kills"),
                "deaths": stat.get("deaths"),
                "assists": stat.get("assists"),
                "impact": stat.get("impact"),
            })

        # Add dash-placeholder rows for roster players missing from
        # results entirely — None for every numeric field, same
        # reasoning as above. verification_card renders these as "—".
        for mp in roster:
            if mp["player_id"] not in results_player_ids:
                player = players_by_id.get(mp["player_id"], {})
                extraction_players.append({
                    "ign": player.get("ign", "?"),
                    "team": mp["team"],
                    "position": None,
                    "is_mvp": False,
                    "kills": None,
                    "deaths": None,
                    "assists": None,
                    "impact": None,
                })

        map_name = (match.get("map_pool") or ["Unknown"])[0]
        final_score = match.get("final_score") or "—"
        extraction = {"players": extraction_players, "final_score": final_score}
        round_data = [{"results": round_results}] if round_results else [{"results": []}]

        has_real_data = len(round_results) > 0
        embed = verification_card(match, round_data, extraction, map_name)
        embed.title = f"Match {match['match_id']} — Inspection"

        data_note = ""
        if not has_real_data:
            data_note = "\n⚠️ No result data stored — only roster shown with dashes."
        elif len(round_results) < 10:
            data_note = f"\n⚠️ Partial data — {len(round_results)} of 10 player results stored."

        entry_method = match.get("entry_method", "ocr")
        method_note = " · ⚠️ Manually entered" if entry_method == "manual" else ""

        embed.description = (
            f"**Status:** `{match['status']}`{method_note}{data_note}\n"
            f"Read-only card for admin inspection — no approve/reject action."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /ign-change (2026-08-15) ─────────────────────────────────
    # Shared command: players change their own IGN (rate-limited, 2 per
    # rolling 7-day window); admins can change any player's IGN by
    # specifying the optional `user` parameter (unlimited, no rate limit).
    # The `user` field only appears in the slash-command picker for
    # members with admin/HOD roles — regular players see only `new_ign`.
    # Deliberately allowed even while in queue — see SESSION_HANDOFF
    # 2026-08-15 for the full reasoning trail.
    @app_commands.command(name="ign-change",
                          description="Change your in-game name (2 per week for players, unlimited for admins)")
    @app_commands.describe(
        new_ign="Your new in-game name exactly as it appears in CODM",
        user="[Admin only] The player whose IGN to change — omit to change your own",
    )
    async def ign_change(self, interaction: discord.Interaction, new_ign: str,
                         user: discord.Member | None = None):
        caller_is_admin = is_admin(interaction)

        # If targeting another player, require admin
        if user is not None and not caller_is_admin:
            await interaction.response.send_message(
                "Only admins can change another player's IGN.", ephemeral=True,
            )
            return

        # Resolve whose IGN we're changing
        target_user = user or interaction.user
        player = await adb.get_player_by_discord_id(target_user.id)
        if not player:
            if user is not None:
                await interaction.response.send_message(f"{target_user.mention} isn't registered.", ephemeral=True)
            else:
                await interaction.response.send_message("You're not registered — use `/register` first.", ephemeral=True)
            return

        old_ign = player["ign"]
        cleaned = new_ign.strip()
        if not cleaned:
            await interaction.response.send_message("New IGN can't be empty.", ephemeral=True)
            return
        if cleaned == old_ign:
            await interaction.response.send_message(f"**{old_ign}** is already the current IGN.", ephemeral=True)
            return

        # Rate limit: 2 per rolling 7-day window, non-admins only,
        # only when changing their own IGN (not when admin targets them)
        if not caller_is_admin:
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            recent_count = await adb.count_recent_ign_changes(player["id"], week_ago)
            if recent_count >= 2:
                await interaction.response.send_message(
                    "You've already changed your IGN twice this week — try again in a few days. "
                    "If this is urgent, ask an admin.",
                    ephemeral=True,
                )
                return

        await adb.update_ign(player["id"], cleaned)

        # Log who initiated: 'self' for player self-service, admin's
        # discord_id when an admin changes someone else's IGN
        if user is not None:
            changed_by = str(interaction.user.id)
        elif caller_is_admin:
            changed_by = str(interaction.user.id)
        else:
            changed_by = "self"
        await adb.log_ign_change(player["id"], old_ign, cleaned, changed_by)

        if user is not None:
            await interaction.response.send_message(
                f"IGN changed: **{old_ign}** → **{cleaned}** ({target_user.mention}) — by {interaction.user.mention}\n"
                f"-# {target_user.mention}, run `/player-stats` to confirm.",
            )
        else:
            await interaction.response.send_message(
                f"IGN changed: **{old_ign}** → **{cleaned}**\n"
                f"-# Run `/player-stats` to confirm.",
            )

    # ── /admin-enter-result (2026-08-15, Approach A: multi-step) ─
    @app_commands.command(name="admin-enter-result",
                          description="[Admin] Manually enter match result data (bypasses OCR)")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)")
    @admin_only()
    async def enter_result(self, interaction: discord.Interaction, match_id: str):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match["status"] != "awaiting_result":
            await interaction.response.send_message(
                f"Match is `{match['status']}` — manual entry only works on `awaiting_result` matches. "
                f"Use `/admin-reset-match` first if needed.",
                ephemeral=True,
            )
            return

        roster = await adb.get_match_players(match["id"])
        if len(roster) != 10:
            await interaction.response.send_message(
                f"Match roster has {len(roster)} players (expected 10) — can't proceed.",
                ephemeral=True,
            )
            return

        view = ManualEntryStep1View(self, match, roster)
        await interaction.response.send_message(
            f"**Manual Entry for {match['match_id']}**\n"
            f"Step 1 of 3 — enter map name, score, and MVP players.",
            view=view,
            ephemeral=True,
        )

    async def _refresh_queue_panel(self, channel: discord.abc.Messageable, queue_key: str) -> bool:
        """Finds the persistent queue-panel message for queue_key in this
        channel (matched by its Join button's custom_id, which is unique
        per queue_key — see RegionQueueView) and re-renders it with the
        current DB state. Built for /admin-queue-clean (2026-08-20): an
        admin cleaning a queue from within the SAME channel the panel
        lives in — the normal workflow — expects the panel to reflect the
        removal immediately, not wait for the next Join/Leave/Reload
        click. Bounded to the last 50 messages; if the panel isn't found
        in that window (wrong channel, or buried under unrelated chat),
        this quietly returns False and the caller falls back to the old
        catches-up-on-next-click behavior rather than erroring out."""
        target_custom_id = f"join_queue_{queue_key}"
        async for msg in channel.history(limit=50):
            if not msg.author.bot or not msg.components:
                continue
            found = any(
                getattr(child, "custom_id", None) == target_custom_id
                for row in msg.components for child in row.children
            )
            if not found:
                continue
            current_queue = await adb.queue_current(queue_key=queue_key)
            view = RegionQueueView(queue_key, self.bot.get_cog("Queue"))
            await view.update_view_state(current_queue)
            embed = make_queue_embed(queue_key, current_queue)
            try:
                await msg.edit(embed=embed, view=view)
                return True
            except discord.HTTPException:
                logger.warning("_refresh_queue_panel: edit failed for message_id=%s queue_key=%s", msg.id, queue_key)
                return False
        return False

    # ── /admin-queue-clean (2026-08-20) ───────────────────────────
    @app_commands.command(name="admin-queue-clean",
                          description="[Admin] Clear AFK/unresponsive players from a queue — whole queue or up to 3 named players")
    @app_commands.describe(
        queue="Which of the 4 queues (EU/AF, NA/Latam, India/ME, Japan)",
        user1="Player to remove (leave all 3 blank to clear the ENTIRE queue)",
        user2="Second player to remove (optional)",
        user3="Third player to remove (optional)",
    )
    @app_commands.choices(queue=[
        app_commands.Choice(name="EU / AF", value="EU_AF"),
        app_commands.Choice(name="NA / Latam", value="NA_LATAM"),
        app_commands.Choice(name="India / ME", value="INDIA_ME"),
        app_commands.Choice(name="Japan", value="JAPAN"),
    ])
    @admin_only()
    async def queue_clean(self, interaction: discord.Interaction, queue: app_commands.Choice[str],
                           user1: discord.Member | None = None, user2: discord.Member | None = None,
                           user3: discord.Member | None = None):
        # Recovery tool for the recurring AFK-at-fill-time problem: players
        # join early, go unresponsive by the time the queue actually hits
        # 10 and a match tries to form. Penalties alone don't solve the
        # immediate "queue is stuck with a dead slot" problem — this does.
        #
        # Panel refresh added 2026-08-20 (live testing feedback): the
        # panel used to just sit stale until the next Join/Leave/Reload
        # click — in practice that meant a player could hit "Start Match"
        # on a panel still showing 10/10 right after an admin clean, and
        # get rejected. _refresh_queue_panel searches THIS channel (the
        # normal workflow: admin runs the command in the same channel the
        # panel lives in) and re-renders it immediately. If the panel
        # isn't found here, this fails quietly — the DB write already
        # succeeded regardless, so nothing is lost, the display just
        # catches up on the next natural click instead.
        await interaction.response.defer(ephemeral=True)
        queue_key = queue.value

        named_users = [u for u in (user1, user2, user3) if u is not None]
        if not named_users:
            removed_count = await adb.queue_clean_all(queue_key)
            await self._refresh_queue_panel(interaction.channel, queue_key)
            await interaction.followup.send(
                f"🧹 Cleared **{removed_count}** player(s) from the **{queue.name}** queue.",
                ephemeral=True,
            )
            return

        current = await adb.queue_current(queue_key=queue_key)
        by_player_id = {row["player_id"]: row for row in current}

        removed, not_found = [], []
        for member in named_users:
            player = await adb.get_player_by_discord_id(member.id)
            if not player or player["id"] not in by_player_id:
                not_found.append(member.mention)
                continue
            await adb.queue_leave(player["id"])
            removed.append(player["ign"])

        if removed:
            await self._refresh_queue_panel(interaction.channel, queue_key)

        lines = []
        if removed:
            lines.append(f"🧹 Removed from **{queue.name}** queue: " + ", ".join(f"**{ign}**" for ign in removed))
        if not_found:
            lines.append("⚠️ Not in that queue (skipped): " + ", ".join(not_found))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ── /admin-queue-replace (2026-08-20) ─────────────────────────
    @app_commands.command(name="admin-queue-replace",
                          description="[Admin] Swap an AFK/unavailable player in a formed match for a new player")
    @app_commands.describe(
        match_id="The match ID (e.g. CQ-0001)",
        old_player="The AFK/unavailable player currently in the match",
        new_player="The new player to bring in, same team as old_player",
    )
    @admin_only()
    async def queue_replace(self, interaction: discord.Interaction, match_id: str,
                             old_player: discord.Member, new_player: discord.Member):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        # Extended through awaiting_result (2026-08-20 planning discussion):
        # in practice most AFK reports surface right after the room code
        # is shared, once teammates start joining the in-game lobby and
        # notice a seat isn't filling — not earlier at forming/awaiting_room
        # when nobody's tried to actually join yet. Cut off at
        # awaiting_result rather than allowing it indefinitely — once the
        # match reaches later states (awaiting_review, completed, etc.)
        # a scoreboard already exists with the original player's IGN on
        # it, and OCR/IGN-resolution is the correct path from there, not
        # a roster swap.
        allowed_statuses = ("forming", "awaiting_room", "awaiting_result")
        if match["status"] not in allowed_statuses:
            await interaction.response.send_message(
                f"Match is `{match['status']}` — replace only works while it's still pre-review "
                f"({', '.join(f'`{s}`' for s in allowed_statuses)}).",
                ephemeral=True,
            )
            return

        old = await adb.get_player_by_discord_id(old_player.id)
        new = await adb.get_player_by_discord_id(new_player.id)
        if not old:
            await interaction.response.send_message(f"{old_player.mention} isn't registered.", ephemeral=True)
            return
        if not new:
            await interaction.response.send_message(f"{new_player.mention} isn't registered.", ephemeral=True)
            return

        roster = await adb.get_match_players(match["id"])
        old_row = next((r for r in roster if r["player_id"] == old["id"]), None)
        if not old_row:
            await interaction.response.send_message(
                f"**{old['ign']}** isn't part of match `{match_id}`.", ephemeral=True
            )
            return
        if any(r["player_id"] == new["id"] for r in roster):
            await interaction.response.send_message(
                f"**{new['ign']}** is already in this match.", ephemeral=True
            )
            return

        team = old_row["team"]
        await interaction.response.defer(ephemeral=True)

        # Pull the incoming player out of ANY queue they might currently
        # be sitting in (2026-08-20 planning discussion) — they're about
        # to be seated in a real match, a stale 'waiting' row would let
        # them get pulled into a second match simultaneously.
        all_queues = await adb.queue_current()
        new_queue_row = next((r for r in all_queues if r["player_id"] == new["id"]), None)
        if new_queue_row:
            await adb.queue_leave(new["id"])

        await adb.remove_match_player(match["id"], old["id"])
        await adb.add_match_player(match["id"], new["id"], team, is_captain=False)

        was_host = match.get("room_code_shared_by") == old["id"]
        if was_host:
            await adb.update_match(match["id"], {"room_code_shared_by": new["id"]})

        # Update channel/VC permissions so the swap is real, not just a
        # DB row change — old player loses access, new player gains it.
        guild = interaction.guild
        text_channel_id = match.get("text_channel_id")
        vc_field = "voice_channel_a_id" if team == "A" else "voice_channel_b_id"
        vc_id = match.get(vc_field)

        text_channel = guild.get_channel(int(text_channel_id)) if guild and text_channel_id else None
        vc = guild.get_channel(int(vc_id)) if guild and vc_id else None
        old_member_obj = guild.get_member(old["discord_id"]) if guild else None
        # new_player is already a resolved discord.Member from the slash
        # command param — no lookup needed.

        for channel_obj in (text_channel, vc):
            if not channel_obj:
                continue
            try:
                if old_member_obj:
                    await channel_obj.set_permissions(old_member_obj, overwrite=None)
                if text_channel_id and channel_obj is text_channel:
                    await channel_obj.set_permissions(new_player, read_messages=True, send_messages=True)
                elif vc_id and channel_obj is vc:
                    await channel_obj.set_permissions(new_player, view_channel=True, connect=True)
            except discord.HTTPException:
                logger.exception(
                    "admin_queue_replace: permission update failed for match_id=%s channel_id=%s",
                    match["id"], getattr(channel_obj, "id", None),
                )

        # Short, plain, in-a-hurry-friendly note — no skill-vote cleanup,
        # teams sort operator picks out themselves (2026-08-20 call).
        if text_channel:
            try:
                host_note = " (new host)" if was_host else ""
                await text_channel.send(
                    f"🔄 **{old['ign']}** replaced by **{new['ign']}**{host_note} (admin). "
                    f"Discuss operator skills with your team."
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"✅ **{old['ign']}** → **{new['ign']}** on match `{match_id}` (Team {team})."
            + (" New player was also removed from a queue they were sitting in." if new_queue_row else "")
            + (" Host reassigned to the new player." if was_host else ""),
            ephemeral=True,
        )

    # ── /admin-map-change (2026-08-20) ────────────────────────────
    @app_commands.command(name="admin-map-change",
                          description="[Admin] Correct a match's map (e.g. after an illegal in-game map switch)")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)", new_map="The corrected map")
    @app_commands.choices(new_map=[app_commands.Choice(name=m, value=m) for m in config.HARDPOINT_MAPS])
    @admin_only()
    async def map_change(self, interaction: discord.Interaction, match_id: str, new_map: app_commands.Choice[str]):
        match = await adb.get_match_by_code(match_id.strip().upper())
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match["status"] in ("completed", "cancelled", "abandoned"):
            await interaction.response.send_message(
                f"Match is already `{match['status']}` — map can no longer be changed.", ephemeral=True
            )
            return

        old_map = (match.get("map_pool") or ["Unknown"])[0]
        if old_map == new_map.value:
            await interaction.response.send_message(
                f"Map is already **{new_map.value}** — nothing to change.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await adb.update_match(match["id"], {"map_pool": [new_map.value]})

        text_channel_id = match.get("text_channel_id")
        text_channel = interaction.guild.get_channel(int(text_channel_id)) if interaction.guild and text_channel_id else None
        if text_channel:
            try:
                await text_channel.send(f"🗺️ Map corrected to **{new_map.value}** by admin.")
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"Map for `{match_id}` changed: **{old_map}** → **{new_map.value}**.", ephemeral=True
        )

    # ── /admin-recompute-stats — COMMENTED OUT (2026-08-15) ──────
    # Superseded by /admin-enter-result routing through approve_match()
    # directly — manual entry no longer needs a separate recompute step.
    # If manual DB recomputation is ever needed again, use the SQL query
    # directly in Supabase (see SESSION_HANDOFF 2026-08-15 Section 5
    # for the corrected, audited version of the bulk recompute query).
    # db.py's recompute_player_career_stats() method is untouched.
    #
    # @app_commands.command(name="admin-recompute-stats", description="[Admin] Force-refresh a player's career stats")
    # @admin_only()
    # async def recompute_stats(self, interaction: discord.Interaction, user: discord.Member):
    #     player = await adb.get_player_by_discord_id(user.id)
    #     if not player:
    #         await interaction.response.send_message(f"{user.mention} isn't registered.", ephemeral=True)
    #         return
    #     await interaction.response.defer(ephemeral=True)
    #     try:
    #         await adb.recompute_player_career_stats(player["id"])
    #     except Exception as exc:
    #         await interaction.followup.send(f"Recompute failed: {exc}", ephemeral=True)
    #         return
    #     await interaction.followup.send(f"Stats recomputed for **{player['ign']}** — check `/player-stats`.", ephemeral=True)

    # @approve.error and @reject.error removed (2026-08-15).
    # @recompute_stats.error removed (2026-08-15) — command commented out above.
    @review_queue.error
    @correct_round.error
    @force_approve.error
    @adjust_reputation.error
    @adjust_mmr.error
    @ign_change.error
    @scrap_match.error
    @reset_match.error
    @match_card.error
    @enter_result.error
    @queue_clean.error
    @queue_replace.error
    @map_change.error
    @dispatch.error
    async def on_admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        else:
            raise error


# ── Manual Entry Views/Modals (Approach A: 3-step) ───────────────
# These live outside the Admin class since Discord.py modal classes
# are standalone — they can't be nested inside a Cog class.

_SCORE_RE = re.compile(r"^(\d+)\s*[-:]\s*(\d+)$")

_STAT_HELP = (
    "One player per line, format:\n"
    "`position ign kills deaths assists hill_time impact`\n"
    "Example:\n"
    "```\n1 folks 41 31 11 89 193\n2 GodLSkullG 79 48 13 45 130\n```\n"
    "position is the in-game rank badge (1-5) shown on the scoreboard —\n"
    "type it exactly as shown, never guessed from anything else.\n"
    "Use seconds for hill_time (e.g. 89 = 1:29). impact can be left as 0 "
    "if the scoreboard doesn't show it — it's cosmetic, never used for MMR "
    "(see mmr_engine.calculate_mmr_change, which is position/win/MVP "
    "based only)."
)


class MatchInfoModal(discord.ui.Modal, title="Match Info"):
    """Step 1: map name, score, MVPs."""
    map_name = discord.ui.TextInput(label="Map Name", placeholder="e.g. Takeoff", required=True, max_length=30)
    score = discord.ui.TextInput(label="Score (A-B)", placeholder="e.g. 246-250", required=True, max_length=10)
    mvp_a = discord.ui.TextInput(label="MVP Team A (IGN)", placeholder="e.g. Master.Fps", required=True, max_length=40)
    mvp_b = discord.ui.TextInput(label="MVP Team B (IGN)", placeholder="e.g. SumitCantSnipe", required=True, max_length=40)

    def __init__(self, parent_view: "ManualEntryStep1View"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        score_match = _SCORE_RE.fullmatch(self.score.value.strip())
        if not score_match:
            await interaction.response.edit_message(
                content=f"❌ Invalid score format: `{self.score.value}`. Use `A-B` (e.g. `246-250`). Try again.",
            )
            return

        pv = self.parent_view
        pv.match_info = {
            "map_name": self.map_name.value.strip(),
            "score_a": int(score_match.group(1)),
            "score_b": int(score_match.group(2)),
            "mvp_a_ign": self.mvp_a.value.strip(),
            "mvp_b_ign": self.mvp_b.value.strip(),
        }
        # Move to step 2
        view = ManualEntryStep2View(pv.cog, pv.match, pv.roster, pv.match_info)
        await interaction.response.edit_message(
            content=(
                f"**Manual Entry for {pv.match['match_id']}** — ✅ Step 1 done\n"
                f"Map: {pv.match_info['map_name']}, Score: {pv.match_info['score_a']}-{pv.match_info['score_b']}\n\n"
                f"Step 2 of 3 — enter Team A player stats.\n{_STAT_HELP}"
            ),
            view=view,
        )


class TeamStatsModal(discord.ui.Modal):
    """Step 2/3: per-team player stats in a text area.

    Format is position-first (2026-08-20 revision): admin types the
    in-game rank badge directly instead of a derived/sorted score.
    Matches the real OCR pipeline's own contract exactly — the vision
    prompt (services/vision_extraction.py) reads position from the
    game-provided rank badge and explicitly says "never derive this
    from score/kills". The old version sorted player-typed `score`
    values to GUESS position, which both diverged from how the real
    pipeline works and turned out to be fragile (a field-count mismatch
    silently shifted every column — see the 2026-08-19 fix history in
    this file's git log). `score` itself is dropped from admin input
    entirely and stored as 0 in match_player_stats: confirmed via
    repo-wide grep that nothing reads it back once position is
    explicit (utils/embeds.py's result_card() also sorts by score, but
    has zero live callers — verified separately, not a live path).

    Placeholder carries format + one example line (2026-08-19 fix,
    format updated 2026-08-20) — the fuller _STAT_HELP text lives in
    the message BEHIND this modal, only visible on desktop. Discord's
    100-char hard limit on placeholder is why the full explanation
    can't also fit here."""
    stats_input = discord.ui.TextInput(
        label="Player Stats (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="position ign kills deaths assists hill_time impact\ne.g: 1 folks 41 31 11 89 193",
        required=True,
        max_length=1000,
    )

    def __init__(self, parent_view, team: str):
        super().__init__(title=f"Team {team} Stats")
        self.parent_view = parent_view
        self.team = team

    async def on_submit(self, interaction: discord.Interaction):
        lines = [l.strip() for l in self.stats_input.value.strip().splitlines() if l.strip()]
        if len(lines) != 5:
            await interaction.response.edit_message(
                content=f"❌ Expected exactly 5 players for Team {self.team}, got {len(lines)}. Try again.",
            )
            return

        parsed = []
        for i, line in enumerate(lines, 1):
            # 2026-08-20 fix: rsplit(maxsplit=5) alone silently corrupts
            # data instead of erroring when a line has the WRONG number
            # of numeric fields (e.g. an admin pasting the old 7-field
            # damage-included format after this modal switched to 6).
            # Found live: a 7-token line still produces exactly 6 parts
            # via rsplit, so len(parts) < 6 never fires — the extra
            # numeric token just gets absorbed into "ign" (e.g.
            # "Master.Fps 76") and every real field silently shifts by
            # one position. The IGN fuzzy-matcher downstream then still
            # resolves "Master.Fps 76" back to the right player (92%+
            # similarity), so nothing ever surfaced as an error — kills/
            # deaths/assists/hill_time/score were just all quietly wrong
            # for that whole team. Fix: split the line into tokens FIRST,
            # count how many TRAILING tokens are purely numeric, and
            # require exactly 5 (kills, deaths, assists, hill_time,
            # score) — not "however many rsplit happened to grab".
            # 2026-08-20 revision: format is now
            # "position ign kills deaths assists hill_time impact" —
            # position leads (the game-provided rank badge, admin-typed
            # directly, never derived), ign is free-text in the middle
            # (may contain spaces), then exactly 5 trailing numeric
            # fields. score is no longer admin-typed at all — see this
            # modal's class docstring for why. Uses the same "count
            # trailing numeric tokens explicitly, don't trust a fixed
            # split count" defense as the 2026-08-19 fix, now also
            # applied to the LEADING position token so a missing/extra
            # field on either end is caught, not silently absorbed.
            tokens = line.split()
            if not tokens or not re.fullmatch(r"[1-5]", tokens[0]):
                await interaction.response.edit_message(
                    content=f"❌ Line {i}: must start with the rank badge position (1-5). Got:\n`{line}`",
                )
                return
            position = int(tokens[0])
            rest = tokens[1:]
            numeric_tail = []
            for tok in reversed(rest):
                if re.fullmatch(r"\d+(\.\d+)?", tok):
                    numeric_tail.insert(0, tok)
                else:
                    break
            if len(numeric_tail) != 5:
                await interaction.response.edit_message(
                    content=f"❌ Line {i}: found {len(numeric_tail)} trailing numeric fields after the IGN, "
                            f"need exactly 5 (kills deaths assists hill_time impact):\n`{line}`",
                )
                return
            ign = " ".join(rest[:len(rest) - 5])
            if not ign:
                await interaction.response.edit_message(
                    content=f"❌ Line {i}: no IGN found between position and stats. Got:\n`{line}`",
                )
                return
            try:
                # hill_time and impact are both numeric(6,2) in the
                # schema (floats) — same as the real OCR pipeline's
                # _HILL_TIME_RE, which explicitly allows decimals for
                # both fields. kills/deaths/assists stay int, matching
                # match_player_stats' integer not null columns.
                kills, deaths, assists = (int(x) for x in numeric_tail[0:3])
                hill_time = float(numeric_tail[3])
                impact = float(numeric_tail[4])
            except ValueError:
                await interaction.response.edit_message(
                    content=f"❌ Line {i}: kills/deaths/assists must be whole numbers, "
                            f"hill_time/impact can have decimals. Got:\n`{line}`",
                )
                return
            parsed.append({
                "ign": ign, "position": position, "kills": kills, "deaths": deaths,
                "assists": assists, "hill_time": hill_time, "impact": impact,
            })

        pv = self.parent_view
        if self.team == "A":
            pv.team_a_stats = parsed
            # Move to step 3
            view = ManualEntryStep3View(pv.cog, pv.match, pv.roster, pv.match_info, pv.team_a_stats)
            await interaction.response.edit_message(
                content=(
                    f"**Manual Entry for {pv.match['match_id']}** — ✅ Steps 1-2 done\n\n"
                    f"Step 3 of 3 — enter Team B player stats.\n{_STAT_HELP}"
                ),
                view=view,
            )
        else:
            pv.team_b_stats = parsed
            # All data collected — process
            await interaction.response.edit_message(
                content=f"**Manual Entry for {pv.match['match_id']}** — processing...",
                view=None,
            )
            await _process_manual_entry(interaction, pv.cog, pv.match, pv.roster,
                                         pv.match_info, pv.team_a_stats, pv.team_b_stats)


class ManualEntryStep1View(discord.ui.View):
    def __init__(self, cog, match: dict, roster: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.match = match
        self.roster = roster
        self.match_info = None

    @discord.ui.button(label="📋 Enter Match Info", style=discord.ButtonStyle.primary)
    async def enter_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MatchInfoModal(self))


class ManualEntryStep2View(discord.ui.View):
    def __init__(self, cog, match: dict, roster: list[dict], match_info: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.match = match
        self.roster = roster
        self.match_info = match_info
        self.team_a_stats = None

    @discord.ui.button(label="📋 Enter Team A Stats", style=discord.ButtonStyle.primary)
    async def enter_team_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TeamStatsModal(self, "A"))


class ManualEntryStep3View(discord.ui.View):
    def __init__(self, cog, match: dict, roster: list[dict], match_info: dict, team_a_stats: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.match = match
        self.roster = roster
        self.match_info = match_info
        self.team_a_stats = team_a_stats
        self.team_b_stats = None

    @discord.ui.button(label="📋 Enter Team B Stats", style=discord.ButtonStyle.primary)
    async def enter_team_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TeamStatsModal(self, "B"))


async def _process_manual_entry(interaction: discord.Interaction, cog, match: dict,
                                 roster: list[dict], match_info: dict,
                                 team_a_stats: list[dict], team_b_stats: list[dict]):
    """Process all collected manual-entry data: resolve IGNs to player IDs,
    compute MMR deltas, write match_round_results + match_player_stats,
    update match status, and post the verification card to the approval
    channel — the exact same downstream path as a normal OCR submission."""

    # Resolve IGNs to player IDs from the roster
    roster_by_ign = {}
    for mp in roster:
        # match_players has player_id but not IGN — need to look up
        player = await adb.get_player_by_id(mp["player_id"])
        if player:
            roster_by_ign[player["ign"].lower()] = {
                "player_id": player["id"],
                "discord_id": player.get("discord_id"),
                "ign": player["ign"],
                "team": mp["team"],
            }

    score_a, score_b = match_info["score_a"], match_info["score_b"]
    winner_team = "A" if score_a > score_b else "B"
    final_score = f"{score_a}:{score_b}"

    round_results = []
    player_stats = []
    extraction_players = []
    errors = []

    for team, team_stats, mvp_ign in [("A", team_a_stats, match_info["mvp_a_ign"]),
                                        ("B", team_b_stats, match_info["mvp_b_ign"])]:
        # 2026-08-20: position now comes directly from what the admin
        # typed (the real rank badge, per TeamStatsModal's format) —
        # no more sorting by a player-typed score to derive it. Guard
        # against duplicate positions within a team explicitly, since
        # uniqueness was previously guaranteed for free by enumerate()
        # over a sorted list; now two admin-typed rows could both claim
        # position 1 by mistake, which needs catching before insert
        # (match_round_results has no unique constraint on
        # (match_id, round_number, team, position) — only on
        # (match_id, round_number, player_id) — so a duplicate wouldn't
        # be caught by the DB either).
        seen_positions = set()
        for stat in team_stats:
            position = stat["position"]
            if position in seen_positions:
                errors.append(f"Team {team}: position {position} used more than once.")
                continue
            seen_positions.add(position)

            # Resolve IGN
            ign_lower = stat["ign"].lower()
            roster_entry = roster_by_ign.get(ign_lower)
            if not roster_entry:
                # Try fuzzy match
                from difflib import get_close_matches
                matches = get_close_matches(ign_lower, roster_by_ign.keys(), n=1, cutoff=0.6)
                if matches:
                    roster_entry = roster_by_ign[matches[0]]
                else:
                    errors.append(f"Team {team}: IGN `{stat['ign']}` not found in roster.")
                    continue

            # NOTE (2026-08-20 fix): deliberately NO check against
            # roster_entry["team"] here. That's match_players.team — a
            # static letter fixed once at queue bootstrap purely for the
            # Discord Defender/Attacker display label. It has no
            # guaranteed relationship to which side a player actually
            # played on in a given round (same reasoning already
            # documented in cogs/match.py's _prepare_round for the OCR
            # path, and the same bug class CQ-7594 fixed there). For
            # manual entry, the admin typing a player into the "Team A"
            # modal step IS the ground truth for this round — that's
            # the human equivalent of the OCR's screen-position
            # grouping. Cross-checking it against the static bootstrap
            # letter rejected valid entries whenever a player's actual
            # round-team differed from their static one, which — per
            # CQ-7594's own findings — is routine, not rare. Found live
            # 2026-08-20: every player in a real match got rejected as
            # "wrong team" this way.

            is_mvp = stat["ign"].lower() == mvp_ign.lower() or (
                roster_entry and roster_entry["ign"].lower() == mvp_ign.lower()
            )
            won = team == winner_team
            mmr_delta = mmr_engine.calculate_mmr_change(position, won, is_mvp)

            round_results.append({
                "match_id": match["id"],
                "round_number": 1,
                "player_id": roster_entry["player_id"],
                "position": position,
                "is_mvp": is_mvp,
                "mmr_delta": mmr_delta,
                "team": team,
            })

            player_stats.append({
                "match_id": match["id"],
                "round_number": 1,
                "player_id": roster_entry["player_id"],
                "kills": stat["kills"],
                "deaths": stat["deaths"],
                "assists": stat["assists"],
                # damage no longer collected (2026-08-19) — the real
                # scoreboard doesn't reliably show it and MMR never uses
                # it (see _STAT_HELP's comment above). Column is
                # nullable, matching the OCR pipeline's own defensive
                # None-when-absent handling in match.py's _prepare_round.
                "damage": None,
                "hill_time": stat["hill_time"],
                # score is no longer admin-typed (2026-08-20) — stored
                # as 0 to satisfy the not-null column. Confirmed via
                # repo-wide grep that nothing reads match_player_stats.
                # score back for any live display or calculation once
                # position is explicit (utils/embeds.py's result_card()
                # also sorts by score, but has zero live callers).
                "score": 0,
                # impact now admin-typed (2026-08-20) — was previously
                # always None here since manual entry never collected it.
                "impact": stat["impact"],
                # NOTE: no "team" key here — match_player_stats has no
                # team column at all (confirmed against schema
                # 2026-08-19, caught live when a manual test INSERT
                # crashed with "column team does not exist"). Team only
                # exists on match_round_results, which round_results
                # (above) already carries correctly.
            })

            extraction_players.append({
                "ign": roster_entry["ign"],
                "team": team,
                "position": position,
                "is_mvp": is_mvp,
                "kills": stat["kills"],
                "deaths": stat["deaths"],
                "assists": stat["assists"],
                "impact": stat["impact"],
            })

    if errors:
        await interaction.edit_original_response(
            content="❌ **Errors found:**\n" + "\n".join(f"• {e}" for e in errors),
        )
        return

    if len(round_results) != 10:
        await interaction.edit_original_response(
            content=f"❌ Expected 10 round-result rows, got {len(round_results)}. Something went wrong during IGN resolution.",
        )
        return

    # Write to DB — same tables the OCR pipeline writes to
    try:
        await adb.insert_match_round_results_batch(round_results)
        await adb.insert_match_player_stats_batch(player_stats)
    except Exception as exc:
        await interaction.edit_original_response(content=f"❌ DB write failed: {exc}")
        return

    # Update match: status → pending_verification, store metadata
    deadline = (discord.utils.utcnow() + timedelta(seconds=config.APPROVAL_TIMEOUT_SECONDS)).isoformat()
    await adb.update_match(match["id"], {
        "status": "pending_verification",
        "winner_team": winner_team,
        "final_score": final_score,
        "approval_deadline": deadline,
        "entry_method": "manual",
    })

    # Post verification card to approval channel — same as the normal flow
    match_cog = cog.bot.get_cog("Match")
    approval_channel = await match_cog._approval_channel() if match_cog else None
    if approval_channel is None and config.RESULT_APPROVAL_CHANNEL_ID:
        approval_channel = cog.bot.get_channel(config.RESULT_APPROVAL_CHANNEL_ID)

    if approval_channel:
        extraction = {"players": extraction_players, "final_score": final_score}
        round_data = [{"results": round_results}]
        from cogs.match import HostApprovalView
        embed = verification_card(match, round_data, extraction, match_info["map_name"])
        embed.description = (
            "⚠️ **Manually entered** — review carefully.\n"
            "MMR values are proposed — nothing is applied until Approve is clicked."
        )
        await approval_channel.send(embed=embed, view=HostApprovalView(match_cog, match["id"]))
        await interaction.edit_original_response(
            content=f"✅ **{match['match_id']}** manually entered. Verification card posted in {approval_channel.mention}.",
        )
    else:
        await interaction.edit_original_response(
            content=f"✅ **{match['match_id']}** manually entered, but approval channel isn't configured. Use `/admin-force-approve` to finalize.",
        )

    # Trigger provisional stats recompute for all 10 players, same as the
    # normal submission flow does at this stage
    for rr in round_results:
        try:
            await adb.recompute_player_career_stats(rr["player_id"])
        except Exception:
            pass


# Dispatch table for /admin-dispatch. Defined after the class so it can
# reference the bound methods by name; each entry is one isolated handler
# — adding a new category (weekly digest, etc.) means one new Choice in
# the command decorator + one new entry here + one new _dispatch_* method,
# nothing existing changes.
_CATEGORY_HANDLERS = {
    "hall_of_fame": Admin._dispatch_hall_of_fame,
}


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))