"""
One-time (or repeatable) migration of member balances from UnbelievaBoat into
this bot's own database, using UnbelievaBoat's official REST API.

This never routes user data through any AI/chat system — it's a direct
server-to-server HTTP call from the bot process to UnbelievaBoat's API, so
there's no "token limit" of any kind to worry about. The only real limits
are UnbelievaBoat's own API rate limits, which this cog respects.

Setup:
    1. Go to https://unbelievaboat.com/api/docs, log in, and generate an
       API token for YOUR server (Applications -> create/select an app ->
       make sure it's authorized on your guild with the Economy permission).
    2. Put that token in your .env file as UNB_API_TOKEN=... (NOT as a
       command argument — command arguments are visible in the channel's
       command-usage line, and this keeps the token out of chat entirely).
    3. Restart the bot so it picks up the new .env value.
    4. Run /migrate-unbelievaboat with dry_run:True first to
       preview the numbers, then again with dry_run:False to apply it.
"""

import os
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.economy import fmt


class UnbMigration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Real, in-code permission gate for every command in this cog.

        @app_commands.default_permissions(administrator=True) is only a DEFAULT:
        a server admin can override it per-role or per-user in Server Settings ->
        Integrations, and the bot would then happily run the single most
        dangerous command it has. This check cannot be overridden."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        raise app_commands.CheckFailure("You need Administrator permission to use this command.")

    # ------------------------------------------------------------------ #
    async def _fetch_all_unb_users(self, token: str, guild_id: int, status_cb=None):
        """Pages through UnbelievaBoat's /guilds/{id}/users endpoint and
        returns a list of (user_id, cash, bank) tuples for every member."""
        headers = {"Authorization": token}
        all_users = []
        page = 1
        total_pages = 1

        async with aiohttp.ClientSession(headers=headers) as session:
            while page <= total_pages:
                url = f"{config.UNB_API_BASE}/guilds/{guild_id}/users"
                params = {"page": page, "limit": config.UNB_PAGE_SIZE}

                async with session.get(url, params=params) as resp:
                    if resp.status == 401:
                        raise PermissionError("UnbelievaBoat rejected the API token (401 Unauthorized).")
                    if resp.status == 403:
                        raise PermissionError(
                            "UnbelievaBoat says this application isn't authorized for this server, "
                            "or is missing the Economy permission (403 Forbidden)."
                        )
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 5))
                        if status_cb:
                            await status_cb(f"Rate limited by UnbelievaBoat — waiting {retry_after:.1f}s...")
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"UnbelievaBoat API error {resp.status}: {body[:200]}")

                    data = await resp.json()
                    users = data.get("users", data if isinstance(data, list) else [])
                    total_pages = data.get("totalPages", 1)

                    for u in users:
                        try:
                            uid = int(u["user_id"])
                            cash = int(u.get("cash", 0) or 0)
                            bank = int(u.get("bank", 0) or 0)
                            all_users.append((uid, cash, bank))
                        except (KeyError, ValueError, TypeError):
                            continue  # skip malformed rows rather than aborting the whole migration

                    if status_cb:
                        await status_cb(f"Fetched page {page}/{total_pages} ({len(users)} users)...")

                    # Respect UnbelievaBoat's rate limit even when we're not being throttled.
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and remaining.isdigit() and int(remaining) <= 1:
                        await asyncio.sleep(float(resp.headers.get("X-RateLimit-Reset-After", 5)))
                    else:
                        await asyncio.sleep(config.UNB_REQUEST_DELAY_SECONDS)

                    page += 1
                    if not users:
                        break

        return all_users

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="migrate-unbelievaboat",
        description="[Admin] Import every member's balance from UnbelievaBoat into this bot.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        mode="'Replace' overwrites balances to match UnbelievaBoat exactly (typical for a one-time migration). "
             "'Add' adds UnbelievaBoat's numbers on top of whatever this bot already has.",
        dry_run="If True (default), only previews the results — nothing is written to this bot's database.",
        source_guild_id="Advanced (bot owner only): pull from a different server ID. Leave blank for this server.",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Replace (recommended for first-time migration)", value="replace"),
        app_commands.Choice(name="Add on top of existing balances", value="add"),
    ])
    async def migrate_unbelievaboat(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        dry_run: bool = True,
        source_guild_id: str = None,
    ):
        token = os.getenv("UNB_API_TOKEN")
        if not token:
            await interaction.response.send_message(
                "No UnbelievaBoat API token configured. Add `UNB_API_TOKEN=...` to this bot's `.env` file "
                "(get one at https://unbelievaboat.com/api/docs) and restart the bot, then try again.",
                ephemeral=True,
            )
            return

        # Pulling balances from ANOTHER guild and importing them here is an
        # arbitrary money-injection path, so it is restricted to the bot owner.
        guild_id = interaction.guild_id
        if source_guild_id:
            if not await self.bot.is_owner(interaction.user):
                await interaction.response.send_message(
                    "Only the bot owner can migrate balances from a different server. "
                    "Leave `source_guild_id` blank to migrate this server.",
                    ephemeral=True,
                )
                return
            if not source_guild_id.strip().isdigit():
                await interaction.response.send_message(
                    "`source_guild_id` must be a numeric Discord server ID.", ephemeral=True
                )
                return
            guild_id = int(source_guild_id)

        await interaction.response.defer(ephemeral=True, thinking=True)
        progress_messages = []

        async def status_cb(msg: str):
            progress_messages.append(msg)

        try:
            unb_users = await self._fetch_all_unb_users(token, guild_id, status_cb=status_cb)
        except PermissionError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        except (RuntimeError, aiohttp.ClientError) as e:
            await interaction.followup.send(f"❌ Migration failed: {e}", ephemeral=True)
            return

        if not unb_users:
            await interaction.followup.send(
                "UnbelievaBoat returned zero users for that server — nothing to migrate. "
                "Double-check the API token is authorized for the right guild.",
                ephemeral=True,
            )
            return

        total_cash = sum(c for _, c, _ in unb_users)
        total_bank = sum(b for _, _, b in unb_users)

        if dry_run:
            embed = discord.Embed(
                title="🔍 Migration Preview (dry run — nothing was changed)",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Users found", value=f"{len(unb_users):,}")
            embed.add_field(name="Total cash", value=fmt(total_cash))
            embed.add_field(name="Total bank", value=fmt(total_bank))
            embed.add_field(name="Mode selected", value=mode.name, inline=False)
            embed.set_footer(text="Re-run with dry_run:False to actually apply this.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        applied = 0
        for uid, cash, bank in unb_users:
            if mode.value == "replace":
                await self.db.set_balance(uid, interaction.guild_id, cash, bank)
            else:
                await self.db.add_cash(uid, interaction.guild_id, cash)
                await self.db.add_bank(uid, interaction.guild_id, bank)
            await self.db.log_transaction(
                uid, interaction.guild_id, "unb_migration", cash + bank,
                f"UnbelievaBoat import ({mode.value})",
            )
            applied += 1

        embed = discord.Embed(title="✅ Migration Complete", color=discord.Color.green())
        embed.add_field(name="Users migrated", value=f"{applied:,}")
        embed.add_field(name="Total cash imported", value=fmt(total_cash))
        embed.add_field(name="Total bank imported", value=fmt(total_bank))
        embed.add_field(name="Mode used", value=mode.name, inline=False)
        embed.set_footer(text="You can now safely revoke/regenerate the UnbelievaBoat API token if you'd like.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "You need Administrator permission for this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(UnbMigration(bot))
