"""
Channel restrictions: keeps noisy command families inside the channels the
server has set aside for them.

Two independently configurable areas:

  * "stock"    — /market, /topmovers, /stockinfo, /portfolio, /stock buy|sell
  * "business" — the public company information commands (/company list,
                 /company info)

Where the allowed channels come from, in priority order:

  1. The per-server list staff set with `/eco-admin channels add`, stored in
     the database. Living in the database means staff can change it without
     redeploying the bot, which matters on a free host.
  2. The fallback lists in config.py (STOCK_CHANNEL_IDS,
     BUSINESS_INFO_CHANNEL_IDS) for servers that would rather hard-code it.

If both are empty the area is unrestricted, so nothing breaks before it has
been set up.

Threads count as their parent channel, so a discussion thread started inside
#stock-market keeps working.
"""

from typing import List

import discord

import config

AREAS = {
    "stock": {
        "label": "Stock market",
        "config_attr": "STOCK_CHANNEL_IDS",
        "column": "stock_channel_ids",
        "emoji": "\U0001f4c8",
        "commands": "`/market`, `/topmovers`, `/stockinfo`, `/portfolio`, `/stock buy`, `/stock sell`",
    },
    "business": {
        "label": "Business information",
        "config_attr": "BUSINESS_INFO_CHANNEL_IDS",
        "column": "business_channel_ids",
        "emoji": "\U0001f3e2",
        "commands": "`/company list`, `/company info`",
    },
}


def config_channels(area: str) -> List[int]:
    """The fallback list from config.py for this area."""
    raw = getattr(config, AREAS[area]["config_attr"], []) or []
    return [int(c) for c in raw]


async def allowed_channels(db, guild_id: int, area: str) -> List[int]:
    """The channel IDs this area is restricted to, or [] for unrestricted."""
    stored = await db.get_channel_lock(guild_id, AREAS[area]["column"])
    if stored:
        return stored
    return config_channels(area)


def _staff_bypass(interaction: discord.Interaction) -> bool:
    """Economy staff can use the commands anywhere when
    config.CHANNEL_LOCK_STAFF_BYPASS is on, so they can test something or answer
    a question without having to move channel."""
    if not getattr(config, "CHANNEL_LOCK_STAFF_BYPASS", True):
        return False
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    from cogs.admin import is_eco_staff

    return is_eco_staff(interaction.user, interaction.guild)


def _channel_ids(interaction: discord.Interaction) -> List[int]:
    """The current channel plus, inside a thread, its parent — so threads under
    an allowed channel are allowed too."""
    channel = interaction.channel
    if channel is None:
        return []
    ids = [channel.id]
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:
        ids.append(parent_id)
    return ids


def describe(area: str, channel_ids: List[int]) -> str:
    if not channel_ids:
        return "anywhere"
    return ", ".join(f"<#{cid}>" for cid in channel_ids)


async def enforce(interaction: discord.Interaction, area: str) -> bool:
    """Returns True if the command may run here. If not, it replies with an
    ephemeral note pointing at the right channel and returns False, so the
    caller can simply `return`.

    The reply is ephemeral on purpose: a player who runs /stock buy in the
    wrong channel should be redirected quietly, not have the mistake posted for
    everyone to see.
    """
    if interaction.guild is None:
        return True

    allowed = await allowed_channels(interaction.client.db, interaction.guild_id, area)
    if not allowed:
        return True
    if any(cid in allowed for cid in _channel_ids(interaction)):
        return True
    if _staff_bypass(interaction):
        return True

    info = AREAS[area]
    where = describe(area, allowed)
    message = (
        f"{info['emoji']} {info['label']} commands only work in {where}.\n"
        f"Head there and try again — this covers {info['commands']}."
    )
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
