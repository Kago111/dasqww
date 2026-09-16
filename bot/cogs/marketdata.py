"""
Price sampling for the website's charts.

WHY THIS EXISTS
---------------
The economy stores each listing's CURRENT price and its price at the last tick.
That is all Discord ever needed: /market shows a number and an arrow. A chart
needs a line, which means a price remembered at regular intervals — so this cog
writes one sample per listing every WEB_PRICE_SAMPLE_SECONDS and prunes
anything older than WEB_PRICE_HISTORY_DAYS.

Samples are written to web_price_samples (webdb.py), not to the economy's own
tables. Losing every row here loses chart history and nothing else — no
balances, no holdings, no trades.

COST
----
One listing at the default 60-second sampling is roughly 30 KB a month. Twenty
listings is under a megabyte. If your host is tight on disk, raise
WEB_PRICE_SAMPLE_SECONDS rather than turning this off: a sparser line is still
a line, but a gap in sampling is a gap in the chart forever.

This cog does nothing at all while the web API is switched off, so it costs
nothing to have loaded on a Discord-only server.
"""

import logging

from discord.ext import commands, tasks

import config
import webapi
import webdb

log = logging.getLogger("beamng-eco-bot.marketdata")

# The loop wakes on a fixed short interval and decides for itself whether a
# sample is due, the same pattern the market tick uses. A tasks.loop interval
# cannot be changed once the cog is loaded, and this way WEB_PRICE_SAMPLE_SECONDS
# is read fresh every time.
WAKE_SECONDS = 15


class MarketData(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_sample = 0.0
        self._last_prune = 0.0
        self.sample_loop.start()

    def cog_unload(self):
        self.sample_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    @tasks.loop(seconds=WAKE_SECONDS)
    async def sample_loop(self):
        if not webapi.enabled():
            return

        import time

        now = time.time()
        interval = max(15, int(getattr(config, "WEB_PRICE_SAMPLE_SECONDS", 60)))
        if now - self._last_sample < interval:
            return
        self._last_sample = now

        try:
            total = 0
            for guild in self.bot.guilds:
                rows = await self.db.list_businesses(guild.id)
                if not rows:
                    continue
                # row: (id, name, owner_id, stock_price, prev_price, total, available, delisted)
                total += await webdb.record_samples(
                    self.db, guild.id, [(row[0], row[3]) for row in rows]
                )
            if total:
                log.debug("Sampled %d listing price(s) for the dashboard.", total)
        except Exception:
            log.exception("Price sampling failed")

        # Pruning is cheap but pointless to run every minute; once an hour keeps
        # the table inside WEB_PRICE_HISTORY_DAYS without competing with trades
        # for the write lock.
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                await webdb.prune_samples(self.db)
            except Exception:
                log.exception("Could not prune old price samples")

    @sample_loop.before_loop
    async def before_sample_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MarketData(bot))
