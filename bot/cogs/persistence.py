"""
Database durability and bot-update safety.

WHY THIS EXISTS
---------------
The economy lives in a single SQLite file, economy.db, running in WAL mode.
WAL mode is the right choice — it survives a host killing the container far
better than the alternatives — but it has one sharp edge that bit this server:

    A committed write goes into economy.db-wal FIRST. It only moves into
    economy.db at a "checkpoint". Until that happens, economy.db on disk is
    genuinely out of date.

So the usual update routine — download the files from the host panel, replace
them with the new version, upload the old economy.db back — quietly throws away
everything still sitting in the WAL. It is never the whole economy, which is
why it looked so strange: just the most recent writes. A couple of players'
balances snap back, and whoever traded shares most recently loses the portfolio
they had just bought.

WHAT THIS COG DOES
------------------
1. Checkpoints the WAL on a short timer (and PRAGMA wal_autocheckpoint in
   database.py does it continuously), so economy.db on disk is never more than
   a few seconds behind reality. Copying it is safe at any moment.

2. Records an economy fingerprint — money supply, shares held, row counts — on
   every clean start and shutdown, INSIDE the database itself. Because it
   travels with the file, the bot can compare the database it has just been
   handed against the one it last had, and shout if the numbers went backwards.

3. Powers /eco-admin db (see cogs/admin.py): prepare-update, verify-update and
   status, which turn the whole thing into two commands staff run either side
   of an update.
"""

import json
import logging
import os
import time

import discord
from discord.ext import commands, tasks

import audit
import config

log = logging.getLogger("beamng-eco-bot.persistence")

# Where the fingerprints live in bot_meta.
LAST_RUN_KEY = "persistence_last_run"        # written continuously while running
PRE_UPDATE_KEY = "persistence_pre_update"    # written by /eco-admin db prepare-update

# Fingerprint fields that must never shrink between one run and the next.
# Money can legitimately fall (fines, taxes, losses), so it is reported but not
# treated as proof of loss on its own; counts and high-water-mark IDs only ever
# go up in a healthy database, so a drop in any of these is hard evidence that
# an older or truncated copy of the file was restored.
MONOTONIC_FIELDS = (
    "players", "transactions", "stock_trades",
    "last_transaction_id", "last_trade_id",
    "vehicles", "listings", "businesses",
)

FIELD_LABELS = {
    "players": "Player accounts",
    "money_supply": "Total money in the economy",
    "cash": "Cash on hand",
    "bank": "Bank balances",
    "savings": "Savings balances",
    "shareholders": "Shareholding records",
    "shares_held": "Shares held by players",
    "portfolio_cost": "Amount invested in stock",
    "listings": "Listed stocks",
    "businesses": "Registered businesses",
    "business_funds": "Business account funds",
    "treasury": "Server treasury",
    "vehicles": "Registered vehicles",
    "active_loans": "Active loans",
    "transactions": "Transaction log entries",
    "stock_trades": "Recorded stock trades",
    "last_transaction_id": "Newest transaction number",
    "last_trade_id": "Newest trade number",
}

MONEY_FIELDS = {
    "money_supply", "cash", "bank", "savings", "portfolio_cost",
    "business_funds", "treasury",
}


def fmt_field(key: str, value: int) -> str:
    return f"${value:,}" if key in MONEY_FIELDS else f"{value:,}"


def compare_fingerprints(before: dict, after: dict) -> dict:
    """Diffs two fingerprints into something a human can act on.

    Returns {"losses": [...], "changes": [...], "verdict": "ok"|"warn"|"lost"}.
    """
    losses, changes = [], []
    for key in FIELD_LABELS:
        if key not in before or key not in after:
            continue
        old, new = int(before[key]), int(after[key])
        if old == new:
            continue
        entry = {
            "key": key,
            "label": FIELD_LABELS[key],
            "before": old,
            "after": new,
            "delta": new - old,
        }
        changes.append(entry)
        if new < old and key in MONOTONIC_FIELDS:
            losses.append(entry)

    verdict = "ok"
    if losses:
        verdict = "lost"
    elif any(c["delta"] < 0 and c["key"] in MONEY_FIELDS for c in changes):
        verdict = "warn"
    return {"losses": losses, "changes": changes, "verdict": verdict}


class Persistence(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_checked = False
        self.checkpoint_loop.start()

    def cog_unload(self):
        self.checkpoint_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    # Keep economy.db itself current.
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=float(getattr(config, "CHECKPOINT_INTERVAL_SECONDS", 30)))
    async def checkpoint_loop(self):
        try:
            folded = await self.db.checkpoint()
        except Exception:
            # checkpoint() already retries and falls back to a PASSIVE
            # checkpoint on ordinary lock contention, so anything that still
            # reaches here is unexpected and worth the full traceback.
            log.exception("WAL checkpoint failed")
            return
        if folded:
            log.debug("Checkpointed %d bytes of WAL into economy.db", folded)

        # Refresh the stored fingerprint so the file always carries an
        # up-to-date record of what it contained the last time the bot was
        # looking at it, even if the host kills us without warning.
        try:
            fingerprint = await self.db.economy_fingerprint()
            fingerprint["saved_at"] = int(time.time())
            await self.db.set_meta(LAST_RUN_KEY, json.dumps(fingerprint))
        except Exception:
            log.exception("Could not record the economy fingerprint")

    @checkpoint_loop.before_loop
    async def before_checkpoint_loop(self):
        await self.bot.wait_until_ready()
        await self.run_startup_check()

    # ------------------------------------------------------------------ #
    # Startup integrity check: did this database come back smaller than the
    # one the bot was last running on?
    # ------------------------------------------------------------------ #
    async def run_startup_check(self):
        if self._startup_checked:
            return
        self._startup_checked = True

        # A WAL left behind at startup means the last shutdown was not clean.
        # Harmless in itself (SQLite recovers it automatically), but worth
        # saying out loud: it is the state in which copying economy.db loses
        # data, so staff should know it happens.
        stale_wal = self.db.wal_size()
        await self.db.checkpoint()
        if stale_wal > 0:
            log.warning(
                "Recovered %.1f KB from the write-ahead log at startup — the previous "
                "shutdown was not clean.", stale_wal / 1024,
            )

        raw = await self.db.get_meta(LAST_RUN_KEY)
        if not raw:
            log.info("No previous economy fingerprint stored — recording the first one now.")
            return

        try:
            before = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("Stored economy fingerprint was unreadable; ignoring it.")
            return

        after = await self.db.economy_fingerprint()
        result = compare_fingerprints(before, after)
        if result["verdict"] != "lost":
            log.info("Startup integrity check passed — the economy matches the last run.")
            return

        when = before.get("saved_at", 0)
        log.error(
            "ECONOMY DATA LOSS DETECTED at startup. The database is smaller than the one "
            "this bot was last running on (%s). Losses: %s",
            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when)) if when else "unknown time",
            ", ".join(f"{e['label']} {e['before']:,} -> {e['after']:,}" for e in result["losses"]),
        )

        description = (
            "The database the bot just started on contains **less** than the one it was "
            "running on before.\n\nThis is what happens when an older copy of `economy.db` "
            "is uploaded after an update. **Restore the newest backup before players carry "
            "on**, or the missing money and shares become permanent."
        )
        if when:
            description += f"\n\nLast known good state: <t:{when}:F>"

        embed = discord.Embed(
            title="\U0001f6a8 Economy data loss detected",
            description=description,
            color=discord.Color.red(),
        )
        for entry in result["losses"][:10]:
            embed.add_field(
                name=entry["label"],
                value=(
                    f"was {fmt_field(entry['key'], entry['before'])}\n"
                    f"now {fmt_field(entry['key'], entry['after'])}\n"
                    f"**{fmt_field(entry['key'], abs(entry['delta']))} missing**"
                ),
                inline=True,
            )
        embed.set_footer(text="Run /eco-admin db status for the full picture.")

        for guild_id in await self.db.get_all_guild_ids():
            try:
                await audit.send_log(self.bot, guild_id, embed)
            except Exception:
                log.exception("Could not post the data-loss warning for guild %s", guild_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Persistence(bot))
