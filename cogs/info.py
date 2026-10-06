"""Interactive player guide (/post-info).

ONE public message with buttons, posted once by an admin. Each player who taps a
button gets a PRIVATE (ephemeral) copy of that page and navigates inside it with
Previous / Back to menu / Next — every later tap EDITS that same private message,
so nothing new piles up in the channel and players never overwrite each other.

Cost of a tap (why this is cheap):
  * ONE interaction response (send_message the first time, edit_message after).
    Interaction responses are not counted against the bot's global REST rate limit.
  * ZERO database calls, ZERO external API calls — pages are static text in memory.
  * ZERO state per user: the page key travels inside the button's custom_id, so 3
    players or 300 cost the same and nothing is remembered between taps.
  * No defer on taps: the response is instant, and defer would add a second call.
    (/post-info itself does defer, because it does a real REST call first.)
Buttons never expire: they are DynamicItems (same pattern as the shield and IGN
panels), matched by custom_id after any restart, with no timeout.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import info_pages as pages
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "secondary": discord.ButtonStyle.secondary,
}


class InfoOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"info_open:(?P<page>[a-z0-9_]+)"):
    """A button on the PUBLIC guide message: opens that page as a private reply."""

    def __init__(self, page: str, label: str | None = None, emoji: str | None = None,
                 style: str = "secondary", row: int | None = None):
        super().__init__(discord.ui.Button(label=label or page, emoji=emoji, style=_STYLES[style],
                                           custom_id=f"info_open:{page}", row=row))
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["page"])

    async def callback(self, interaction: discord.Interaction):
        await _show(interaction, self.page, edit=False)


class InfoGoButton(discord.ui.DynamicItem[discord.ui.Button], template=r"info_go:(?P<page>[a-z0-9_]+)"):
    """A button inside the PRIVATE guide: switches that same private message to another page."""

    def __init__(self, page: str, label: str | None = None, emoji: str | None = None,
                 style: str = "secondary", row: int | None = None, disabled: bool = False):
        super().__init__(discord.ui.Button(label=label or page, emoji=emoji, style=_STYLES[style],
                                           custom_id=f"info_go:{page}", row=row, disabled=disabled))
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["page"])

    async def callback(self, interaction: discord.Interaction):
        await _show(interaction, self.page, edit=True)


def build_view(mode: str, page: str) -> discord.ui.View:
    """mode 'open' = the public message's buttons, 'go' = the private guide's buttons.
    The home page is the button grid; every other page gets Previous / Menu / Next."""
    view = discord.ui.View(timeout=None)
    if page == pages.HOME:
        button_cls = InfoOpenButton if mode == "open" else InfoGoButton
        for i, key in enumerate(pages.ORDER):
            label, emoji, style = pages.BUTTONS[key]
            view.add_item(button_cls(key, label, emoji, style, row=0 if i < 5 else 1))
        return view
    prev_key, next_key = pages.neighbours(page)
    view.add_item(InfoGoButton(prev_key or page, "Previous", "◀️", "secondary", row=0, disabled=prev_key is None))
    view.add_item(InfoGoButton(pages.HOME, "Back to menu", "🏠", "primary", row=0))
    view.add_item(InfoGoButton(next_key or page, "Next", "▶️", "secondary", row=0, disabled=next_key is None))
    return view


async def _show(interaction: discord.Interaction, page: str, *, edit: bool) -> None:
    if page not in pages.PAGE_KEYS:          # a button from an older version of the guide
        page = pages.HOME
    embed = pages.build_embed(page)
    view = build_view("go", page)
    try:
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    except discord.NotFound:
        # The 3-second window passed (bot was busy) or the private message was dismissed.
        logger.info("info guide: interaction expired (page=%s)", page)
    except discord.HTTPException:
        logger.warning("info guide: could not show page %s", page, exc_info=True)


class InfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_dynamic_items(InfoOpenButton, InfoGoButton)

    @app_commands.command(name="post-info", description="Admin: post the interactive ChampQueue guide")
    @app_commands.describe(
        channel="Where to post it (default: this channel)",
        message_id="Update an existing guide message in that channel instead of posting a new one",
    )
    @app_commands.check(lambda i: is_admin(i))
    async def post_info(self, interaction: discord.Interaction,
                        channel: discord.TextChannel | None = None, message_id: str | None = None) -> None:
        # defer: this command makes a real REST call first, which could outlast the 3-second window
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        embed, view = pages.build_embed(pages.HOME), build_view("open", pages.HOME)
        try:
            if message_id:
                try:
                    msg = await target.fetch_message(int(message_id))
                except (ValueError, discord.NotFound):
                    await interaction.followup.send("I can't find that message in that channel.", ephemeral=True)
                    return
                if msg.author.id != self.bot.user.id:
                    await interaction.followup.send("That message wasn't posted by me, so I can't edit it.", ephemeral=True)
                    return
                await msg.edit(embed=embed, view=view)
                await interaction.followup.send(f"Guide updated: {msg.jump_url}", ephemeral=True)
            else:
                msg = await target.send(embed=embed, view=view)
                await interaction.followup.send(f"Guide posted: {msg.jump_url}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to post or edit there.", ephemeral=True)
        except discord.HTTPException:
            logger.warning("post-info failed", exc_info=True)
            await interaction.followup.send("Discord rejected that. Try again in a moment.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InfoCog(bot))
