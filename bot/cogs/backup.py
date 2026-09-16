"""
Automatic database backups to a Discord channel.

Free hosting is cheap because it is disposable: most free tiers wipe the
container's filesystem on every redeploy, and some wipe it on an ordinary
restart. If economy.db lives on that filesystem, one redeploy takes every
player's money with it.

This cog uploads a consistent snapshot of the database to a private Discord
channel on a timer, so the newest backup is always one download away even if
the host loses the disk entirely. Turn it on by setting BACKUP_CHANNEL_ID in
your .env (or in the host's environment variables) to a channel only staff can
see. Timing lives in config.py.

Restoring: download the attachment, rename it to economy.db, and put it where
DATABASE_PATH points before starting the bot.
"""

import os
import time
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.admin import is_eco_staff

log = logging.getLogger("beamng-eco-bot.backup")

BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "").strip()


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.backup_tick.start()

    def cog_unload(self):
        self.backup_tick.cancel()

    @property
    def db(self):
        return self.bot.db

    def _channel_id(self):
        if not BACKUP_CHANNEL_ID.isdigit():
            return None
        return int(BACKUP_CHANNEL_ID)

    async def _send_backup(self, reason: str) -> str:
        """Uploads a snapshot. Returns a short human-readable result."""
        channel_id = self._channel_id()
        if not channel_id:
            return "No backup channel is configured (set BACKUP_CHANNEL_ID)."

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                return f"Could not open the backup channel: {exc}"

        path = await self.db.snapshot()
        size = os.path.getsize(path)
        # Discord rejects oversized uploads; a plain warning beats a stack trace.
        if size > 9 * 1024 * 1024:
            return (
                f"The database is {size / 1024 / 1024:.1f} MB, too large to upload to Discord. "
                "Move it to a host with a persistent disk."
            )

        stamp = time.strftime("%Y-%m-%d_%H-%M", time.gmtime())
        try:
            await channel.send(
                content=f"🗄️ Economy database backup — {stamp} UTC ({reason}, {size / 1024:.0f} KB)",
                file=discord.File(path, filename=f"economy-{stamp}.db"),
            )
        except discord.HTTPException as exc:
            return f"Upload failed: {exc}"
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return f"Backup uploaded to <#{channel_id}>."

    async def send_backup(self, reason: str) -> str:
        """Public entry point so other cogs (e.g. the pre-reset safety backup in
        /eco-admin reset-economy) can force a snapshot."""
        return await self._send_backup(reason)

    # ------------------------------------------------------------------ #
    # Timer. Like the other loops it checks a persisted timestamp instead of
    # trusting the loop's own clock, so a host that restarts the bot ten times
    # an hour still produces exactly one backup per interval.
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=10)
    async def backup_tick(self):
        if not getattr(config, "BACKUP_ENABLED", True) or not self._channel_id():
            return
        now = int(time.time())
        interval = max(1, int(getattr(config, "BACKUP_INTERVAL_HOURS", 6))) * 3600
        last = int(await self.db.get_meta("last_backup", 0) or 0)
        if last and now - last < interval:
            return
        await self.db.set_meta("last_backup", now)
        result = await self._send_backup("scheduled")
        log.info("Scheduled backup: %s", result)

    @backup_tick.before_loop
    async def before_backup_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="backup-now",
        description="Staff only: upload a database backup to the backup channel right now.",
    )
    @app_commands.default_permissions()
    async def backup_now(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if not is_eco_staff(interaction.user, interaction.guild):
            raise app_commands.CheckFailure("You need an economy staff role to use this command.")

        await interaction.response.defer(ephemeral=True)
        result = await self._send_backup(f"manual by {interaction.user.display_name}")
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))
