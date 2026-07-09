import discord
from discord import app_commands

import config


def is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == config.ADMIN_ROLE_ID for role in interaction.user.roles)


def admin_only():
    def predicate(interaction: discord.Interaction) -> bool:
        return is_admin(interaction)
    return app_commands.check(predicate)
