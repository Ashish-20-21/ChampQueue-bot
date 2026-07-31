import asyncio
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
from database.db import db, adb
from services import reputation, mmr_engine
from utils.permissions import admin_only

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

    @app_commands.command(name="admin-approve", description="[Admin] Approve a pending player by their Discord user")
    @admin_only()
    async def approve(self, interaction: discord.Interaction, user: discord.Member):
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
            return
        await adb.approve_player(player["id"], str(interaction.user.id))
        await interaction.response.send_message(f"Approved **{player['ign']}** ({user.mention}).", ephemeral=True)

    @app_commands.command(name="admin-reject", description="[Admin] Reject a pending registration")
    @admin_only()
    async def reject(self, interaction: discord.Interaction, user: discord.Member):
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
            return
        await adb.reject_player(player["id"])
        await interaction.response.send_message(f"Rejected registration for **{player['ign']}**.", ephemeral=True)

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

    @app_commands.command(name="admin-correct-round", description="[Admin] Correct one player's position/MVP for a single round (RO3-aware)")
    @app_commands.describe(match_id="The match ID (e.g. CQ-0001)", round_number="Which round (1-3)",
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

    @app_commands.command(name="admin-force-approve", description="[Admin] Approve a match once all 30 round-result rows exist")
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

    @app_commands.command(name="admin-recompute-stats", description="[Admin] Force-refresh a player's career stats right now (no waiting for their next match)")
    @admin_only()
    async def recompute_stats(self, interaction: discord.Interaction, user: discord.Member):
        # Manual escape hatch for the provisional-stats reform (2026-07-29,
        # migration_011). Normally a player's career numbers refresh
        # automatically the next time any of their matches reaches
        # pending_verification or completed — this just lets an admin
        # force that refresh immediately after a manual match_player_stats/
        # match_round_results DB fix, without needing the player to queue
        # again first. Does not touch MMR — that's still admin-adjust-mmr
        # or a direct query, unchanged.
        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} isn't registered.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await adb.recompute_player_career_stats(player["id"])
        except Exception as exc:
            await interaction.followup.send(f"Recompute failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Stats recomputed for **{player['ign']}** — check `/player-stats`.", ephemeral=True)

    @app_commands.command(name="admin-recompute-stats-bulk", description="[Admin] Force-refresh career stats for up to 10 players at once")
    @app_commands.describe(
        user1="Player 1", user2="Player 2", user3="Player 3", user4="Player 4", user5="Player 5",
        user6="Player 6", user7="Player 7", user8="Player 8", user9="Player 9", user10="Player 10",
    )
    @admin_only()
    async def recompute_stats_bulk(
        self, interaction: discord.Interaction,
        user1: discord.Member, user2: discord.Member | None = None, user3: discord.Member | None = None,
        user4: discord.Member | None = None, user5: discord.Member | None = None, user6: discord.Member | None = None,
        user7: discord.Member | None = None, user8: discord.Member | None = None, user9: discord.Member | None = None,
        user10: discord.Member | None = None,
    ):
        # Batch version of /admin-recompute-stats, added 2026-07-29 —
        # doing this one-by-one after a bulk DB fix was time-consuming
        # for the admin. Only user1 is required; the rest are optional
        # so this works for anywhere from 1 to 10 players in one call.
        # Discord slash commands cap at 25 options and have no native
        # array/list parameter type, so 10 named optional discord.Member
        # slots is the standard pattern for "up to N of the same thing"
        # — same reasoning as why /match-submit takes 3 separate
        # attachment parameters instead of a list.
        #
        # Deliberately sequential, not gathered concurrently: each
        # recompute is a real RPC call, and processing one-by-one means
        # a single player's failure is isolated and reported by name
        # instead of asyncio.gather's default all-or-nothing exception
        # behavior silently obscuring which specific player failed.
        candidates = [user1, user2, user3, user4, user5, user6, user7, user8, user9, user10]
        users = [u for u in candidates if u is not None]

        # Duplicate mentions (admin fat-fingering the same user into two
        # slots) would just mean a harmless double-recompute — de-dupe
        # by id anyway so the summary counts and error list stay clean.
        seen_ids = set()
        deduped = []
        for u in users:
            if u.id not in seen_ids:
                seen_ids.add(u.id)
                deduped.append(u)
        users = deduped

        await interaction.response.defer(ephemeral=True)

        succeeded, failed, not_registered = [], [], []
        for u in users:
            player = await adb.get_player_by_discord_id(u.id)
            if not player:
                not_registered.append(u.mention)
                continue
            try:
                await adb.recompute_player_career_stats(player["id"])
                succeeded.append(player["ign"])
            except Exception as exc:
                failed.append(f"{player['ign']} ({exc})")

        lines = [f"Recomputed **{len(succeeded)}/{len(users)}** players."]
        if succeeded:
            lines.append("✅ " + ", ".join(succeeded))
        if failed:
            lines.append("❌ Failed: " + "; ".join(failed))
        if not_registered:
            lines.append("⚠️ Not registered: " + ", ".join(not_registered))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @approve.error
    @reject.error
    @review_queue.error
    @correct_round.error
    @force_approve.error
    @adjust_reputation.error
    @adjust_mmr.error
    @scrap_match.error
    @recompute_stats.error
    @recompute_stats_bulk.error
    async def on_admin_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))