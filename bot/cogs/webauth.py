"""
/weblogin, /weblogout, /dashboard — how a player signs in to the website.

WHY CODES AND NOT OAUTH
-----------------------
A Discord OAuth app would mean registering redirect URLs, keeping a client
secret on the website, and a player trusting a third-party page with their
Discord account. This bot already knows exactly who ran a slash command, which
is the only fact the website needs. So: run /weblogin, get a one-time code
privately, paste it into the site, and the site trades it for a session token.

The code is shown ephemerally (only the player who ran the command can see it),
expires in ten minutes, and is worth exactly one session. Asking for a new one
invalidates the old one.

These commands are inert when the web API is switched off — they say so rather
than handing out codes for a site that cannot accept them.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import webapi
import webdb

log = logging.getLogger("beamng-eco-bot.webauth")


class WebAuth(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    def _offline_message(self) -> str:
        return (
            "🌐 The web dashboard isn't switched on for this server yet.\n"
            "Staff: set `WEB_API_KEY` in the host's environment variables and restart the bot."
        )

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="weblogin",
        description="Get a one-time code to sign in to the stock market website.",
    )
    async def weblogin(self, interaction: discord.Interaction):
        if not webapi.enabled():
            await interaction.response.send_message(self._offline_message(), ephemeral=True)
            return

        code, expires_at = await webdb.create_login_code(
            self.db, interaction.user.id, interaction.guild_id
        )
        url = webapi.dashboard_url()
        where = f"\n\n**Dashboard:** {url}" if url else ""

        embed = discord.Embed(
            title="🔑 Your sign-in code",
            description=(
                f"```\n{code}\n```\n"
                f"Paste this into the website's sign-in box. It expires <t:{expires_at}:R> "
                "and can only be used once."
                f"{where}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text="Never share this code. Anyone who has it can trade with your money "
                 "until you run /weblogout."
        )
        # Ephemeral: the code is a credential, and a market channel is a public
        # place. Only the player who ran the command ever sees this.
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="weblogout",
        description="Sign out of the website everywhere, on every device.",
    )
    async def weblogout(self, interaction: discord.Interaction):
        revoked = await webdb.revoke_all_sessions(
            self.db, interaction.user.id, interaction.guild_id
        )
        if revoked:
            message = (
                f"✅ Signed out of **{revoked}** browser session(s). "
                "Run `/weblogin` when you want to trade on the site again."
            )
        else:
            message = "You weren't signed in to the website anywhere."
        await interaction.response.send_message(message, ephemeral=True)

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="dashboard",
        description="Get the link to the stock market website.",
    )
    async def dashboard(self, interaction: discord.Interaction):
        url = webapi.dashboard_url()
        if not url:
            await interaction.response.send_message(
                "No dashboard URL has been set yet. Staff: set `WEB_DASHBOARD_URL` "
                "(or `WEB_DASHBOARD_URL` in `config.py`) to the published site address.",
                ephemeral=True,
            )
            return

        sessions = await webdb.count_sessions(self.db, interaction.user.id, interaction.guild_id)
        status = (
            f"You're signed in ({sessions} active session(s))."
            if sessions else
            "You're not signed in — run `/weblogin` to get a code."
        )
        await interaction.response.send_message(
            f"📈 **Stock market dashboard**\n{url}\n\n{status}", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(WebAuth(bot))
