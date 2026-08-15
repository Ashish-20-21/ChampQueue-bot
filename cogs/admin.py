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

    @app_commands.command(name="admin-ign-change", description="[Admin] Change a player's IGN — use when they've renamed after registering/queueing")
    @admin_only()
    async def ign_change(self, interaction: discord.Interaction, user: discord.Member, new_ign: str):
        # Restricted to a dedicated channel, same fail-open-if-unset pattern
        # as RESULT_UPLOAD/APPROVAL_CHANNEL_ID — see config.py.
        if config.IGN_CHANGE_CHANNEL_ID and interaction.channel_id != config.IGN_CHANGE_CHANNEL_ID:
            await interaction.response.send_message(
                f"This only works in <#{config.IGN_CHANGE_CHANNEL_ID}>.", ephemeral=True,
            )
            return

        player = await adb.get_player_by_discord_id(user.id)
        if not player:
            await interaction.response.send_message(f"{user.mention} hasn't registered.", ephemeral=True)
            return

        old_ign = player["ign"]
        cleaned_new_ign = new_ign.strip()
        if not cleaned_new_ign:
            await interaction.response.send_message("New IGN can't be empty.", ephemeral=True)
            return
        if cleaned_new_ign == old_ign:
            await interaction.response.send_message(f"**{old_ign}** is already their IGN — nothing to change.", ephemeral=True)
            return

        # No history table by design (2026-08-08 decision) — this message
        # IS the audit trail, hence public in-channel rather than ephemeral.
        await adb.update_ign(player["id"], cleaned_new_ign)
        await interaction.response.send_message(
            f"IGN changed: **{old_ign}** → **{cleaned_new_ign}**  ({user.mention}) — by {interaction.user.mention}\n"
            f"-# {user.mention}, run `/player-stats` to confirm.",
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

    # @approve.error and @reject.error removed here (2026-08-15) — the
    # commands they referenced are commented out above; leaving these
    # decorators in place caused a NameError at cog-load time
    # ("name 'approve' is not defined"), confirmed live via a real bot
    # restart. If admin-approve/admin-reject are ever un-commented, these
    # two lines must come back too.
    @review_queue.error
    @correct_round.error
    @force_approve.error
    @adjust_reputation.error
    @adjust_mmr.error
    @ign_change.error
    @scrap_match.error
    @recompute_stats.error
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