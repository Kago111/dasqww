"""
Entry point for the BeamNG RP Economy Bot.

Local usage:
    1. Copy .env.example to .env, then set DISCORD_TOKEN and GUILD_ID
    2. pip install -r requirements.txt
    3. python main.py

Free 24/7 hosting: see HOSTING.md. The bot is built to survive the way free
hosts behave — frequent restarts, disposable filesystems, and platforms that
only keep a service alive while it answers HTTP requests:

  * DATABASE_PATH points the database at a persistent disk.
  * A keep-alive web server binds $PORT when the host provides one.
  * Slash commands are only re-synced when they actually changed, so a host
    that restarts the bot every few minutes cannot burn Discord rate limits.
  * SIGTERM is handled, so a container being recycled closes the database
    cleanly instead of losing the last few seconds of writes.
"""

import os
import sys
import signal
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import audit
import whitelist
from database import Database, DB_PATH
from keepalive import KeepAliveServer

load_dotenv()

# Panel-style hosts (bot-hosting.net, Pterodactyl and friends) build the start
# command for you and usually run a plain `python main.py`, with no -u. Without
# this, stdout is block-buffered because the console is a pipe and the live log
# stays empty for minutes at a time, which makes a healthy bot look dead.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

TOKEN = os.getenv("DISCORD_TOKEN")
# Required: your server's ID. Commands are always synced guild-scoped so new or
# changed commands appear instantly (a global sync can take up to an hour).
GUILD_ID = os.getenv("GUILD_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,  # free hosts capture stdout; unbuffered below
)
log = logging.getLogger("beamng-eco-bot")

INTENTS = discord.Intents.default()
INTENTS.members = True  # needed to resolve users for admin/police commands

EXTENSIONS = (
    "cogs.economy", "cogs.jobs", "cogs.vehicles", "cogs.legal", "cogs.stocks",
    "cogs.companies", "cogs.migration", "cogs.admin", "cogs.backup",
    "cogs.persistence",
)

# Web dashboard: sign-in codes and the price history its charts are drawn from.
# Both are inert while the API is switched off, and the files may not be present
# at all (they ship separately). A host is not the place to discover that a
# missing optional file takes the whole economy offline, so these are loaded
# best-effort and their absence is just a log line.
OPTIONAL_EXTENSIONS = ("cogs.webauth", "cogs.marketdata")


def command_signature(tree: app_commands.CommandTree) -> str:
    """A stable fingerprint of every command, its description and its options.

    Re-syncing on every boot is fine on a machine that stays up for weeks. On a
    free host that restarts the bot constantly it is a good way to hit Discord's
    sync rate limit and end up with no commands at all, so we only call sync
    when this fingerprint changes.
    """
    parts = []
    for command in sorted(tree.walk_commands(), key=lambda c: c.qualified_name):
        bits = [command.qualified_name, getattr(command, "description", "")]
        for param in getattr(command, "parameters", []):
            bits.append(f"{param.name}:{param.type}:{param.required}:{param.description}")
        parts.append("|".join(str(b) for b in bits))
    return "\n".join(parts)


class EconomyTree(app_commands.CommandTree):
    """Command tree with the whitelist applied to every command.

    Putting the check here rather than on each command means any command added
    later is covered automatically, and a non-whitelisted member's interaction
    stops before any cog code runs — so no account is ever created for them and
    they never appear in the economy at all.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await whitelist.enforce(interaction)


class EconomyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!eco-legacy-unused!",
            intents=INTENTS,
            tree_cls=EconomyTree,
            # Free tiers are usually capped around 512 MB of RAM. Downloading
            # and caching the full member list of a large server at startup is
            # the single biggest thing that would push past it, and nothing
            # here needs it: every command receives the member it acts on
            # directly from Discord.
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,  # the bot never reads message history
        )
        self.db = Database()
        self.keepalive = KeepAliveServer(self)

    async def setup_hook(self):
        await self.db.connect()
        log.info("Database connected (%s).", os.path.abspath(DB_PATH))

        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        for ext in OPTIONAL_EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("Loaded optional extension %s", ext)
            except commands.ExtensionNotFound:
                log.info("Optional extension %s is not installed — skipping.", ext)
            except Exception:
                log.exception("Optional extension %s failed to load — continuing without it.", ext)

        await self.keepalive.start()
        await self._sync_commands_if_changed()

    async def _sync_commands_if_changed(self):
        guild = discord.Object(id=int(GUILD_ID))
        self.tree.copy_global_to(guild=guild)

        signature = command_signature(self.tree)
        key = f"cmd_signature_{GUILD_ID}"
        previous = await self.db.get_meta(key, "")

        if previous == signature and os.getenv("FORCE_SYNC", "").lower() not in ("1", "true", "yes"):
            log.info("Slash commands unchanged since last start — skipping sync.")
            return

        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s.", len(synced), GUILD_ID)
            # Clear any commands left over from a previous global sync so they
            # can't linger as stale duplicates alongside the guild-scoped ones.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            await self.db.set_meta(key, signature)
        except discord.HTTPException as exc:
            # Never crash on a failed sync: the commands registered on Discord's
            # side from the previous run still work, and crashing here would put
            # a free host into a restart loop.
            log.warning("Command sync failed (%s). Existing commands stay in place.", exc)

    async def close(self):
        await self.keepalive.stop()
        await self.db.close()
        await super().close()


bot = EconomyBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Last-resort handler so unexpected errors never leave a command hanging."""
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "You don't have permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"That command is on cooldown. Try again in {error.retry_after:.0f}s."
    else:
        command_name = interaction.command.qualified_name if interaction.command else "?"
        log.error("Unhandled error in /%s", command_name, exc_info=error)
        message = "Something went wrong running that command. Please try again."

    await audit.log_command_error(bot, interaction, error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    """Mirrors every successful command into the guild's audit log channel."""
    await audit.log_command(bot, interaction, command)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="the pump prices | /balance"
    ))


@bot.event
async def on_resumed():
    log.info("Reconnected to Discord and resumed the session.")


async def run():
    """Starts the bot and shuts it down cleanly when the host recycles us."""
    loop = asyncio.get_running_loop()

    async def shutdown(sig_name):
        log.info("Received %s — closing the database and shutting down cleanly.", sig_name)
        await bot.close()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda s=sig_name: asyncio.create_task(shutdown(s)))
        except NotImplementedError:
            pass  # Windows

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Locally: copy .env.example to .env and fill it in. "
            "On a host: add DISCORD_TOKEN in the panel's environment variables / secrets."
        )
    if not GUILD_ID:
        raise SystemExit(
            "GUILD_ID is not set. Add GUILD_ID=<your server ID> to .env (or to your host's "
            "environment variables).\n"
            "Enable Developer Mode in Discord (User Settings -> Advanced), then right-click "
            "your server icon and choose \"Copy Server ID\"."
        )
    if not GUILD_ID.strip().isdigit():
        raise SystemExit(f"GUILD_ID must be a numeric Discord server ID, got: {GUILD_ID!r}")

    try:
        asyncio.run(run())
    except discord.LoginFailure:
        raise SystemExit(
            "Discord rejected the token. Regenerate it in the Developer Portal and update "
            "DISCORD_TOKEN on your host."
        )
    except KeyboardInterrupt:
        pass
