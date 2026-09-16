"""
Whitelist: decides who may use the bot at all.

When config.WHITELIST_ENABLED is on, a member needs one of the whitelisted
roles before any command will run for them. Because the check runs before the
command does, a non-whitelisted member never touches the economy — no account
row is created for them, so they have no balance, appear on no leaderboard and
can't be paid.

Where the allowed roles come from, in priority order:

  1. The per-server list staff set with `/eco-admin whitelist add`, stored in
     the database so it survives restarts and redeploys on a free host.
  2. The fallback list in config.py (WHITELIST_ROLE_IDS).

Economy staff and Administrators bypass the whitelist while
config.WHITELIST_STAFF_BYPASS is True, and the server owner always bypasses it,
so a misconfigured list can never lock the server out of its own bot.
"""

from typing import List

import discord

import config

COLUMN = "whitelist_role_ids"


def enabled() -> bool:
    return bool(getattr(config, "WHITELIST_ENABLED", False))


def config_roles() -> List[int]:
    """The fallback role list from config.py."""
    return [int(r) for r in (getattr(config, "WHITELIST_ROLE_IDS", []) or [])]


async def allowed_roles(db, guild_id: int) -> List[int]:
    """Role IDs allowed to use the bot, or [] if none have been set anywhere."""
    stored = await db.get_channel_lock(guild_id, COLUMN)
    if stored:
        return stored
    return config_roles()


def bypasses(member: discord.Member, guild: discord.Guild) -> bool:
    """Who is exempt from the whitelist."""
    if guild is not None and guild.owner_id == member.id:
        return True  # the server owner can never be locked out
    if not getattr(config, "WHITELIST_STAFF_BYPASS", True):
        return False
    from cogs.admin import is_eco_staff

    return is_eco_staff(member, guild)


def describe(role_ids: List[int]) -> str:
    if not role_ids:
        return "nobody (no whitelist role set yet)"
    return ", ".join(f"<@&{rid}>" for rid in role_ids)


async def check(interaction: discord.Interaction) -> bool:
    """True if this member may use the bot. Does not reply."""
    if not enabled():
        return True
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return True
    if bypasses(interaction.user, interaction.guild):
        return True

    allowed = await allowed_roles(interaction.client.db, interaction.guild_id)
    if not allowed:
        # Nothing configured: the whitelist is on but unusable. Refusing
        # everyone here would look like the bot is broken, so say so loudly in
        # the reply instead (see enforce()).
        return False
    member_roles = {role.id for role in interaction.user.roles}
    return any(rid in member_roles for rid in allowed)


async def enforce(interaction: discord.Interaction) -> bool:
    """Returns True if the command may run. Otherwise replies privately and
    returns False."""
    if await check(interaction):
        return True

    # Autocomplete interactions cannot be replied to with a message; silently
    # returning no suggestions is the right behaviour there.
    if interaction.type is discord.InteractionType.autocomplete:
        return False

    allowed = await allowed_roles(interaction.client.db, interaction.guild_id)
    if not allowed:
        message = (
            "🔒 The whitelist is switched on, but no whitelist role has been set yet, "
            "so nobody can use the bot.\n"
            "A staff member needs to run `/eco-admin whitelist add`."
        )
    else:
        message = getattr(config, "WHITELIST_MESSAGE", "You need to be whitelisted to use this bot.")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass
    return False
