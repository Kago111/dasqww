import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import channels
import config
import stockhistory
import stockimpact
from amounts import parse_count
from cogs.economy import fmt, format_duration, send_error


def fmt_price(price: float) -> str:
    return f"${price:,.2f}"


def pct_change(price: float, prev: float) -> float:
    if prev == 0:
        return 0.0
    return ((price - prev) / prev) * 100


def trend_arrow(change: float) -> str:
    if change > 0.01:
        return "🔺"
    if change < -0.01:
        return "🔻"
    return "➖"


def fmt_cap(market_cap: float) -> str:
    """Compact formatting for market cap, e.g. $1.2M, $850K."""
    if market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:,.2f}M"
    if market_cap >= 1_000:
        return f"${market_cap / 1_000:,.1f}K"
    return f"${market_cap:,.2f}"


class OrderRejected(Exception):
    """Raised inside a locked trade transaction to refuse the order. The
    transaction rolls back (nothing was charged, no cooldown was spent) and
    the message is shown to the player.

    `code` and `details` exist for the website, which needs to react to a
    refusal rather than just print it: a "cooldown" refusal carries the
    seconds left so the page can show a ticking countdown and grey the button
    out, where Discord only ever needed the sentence. Discord's own handlers
    use str(exc) and are unaffected by either.
    """

    def __init__(self, message: str, code: str = "rejected", **details):
        super().__init__(message)
        self.code = code
        self.details = details


class Stocks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One asyncio.Lock per business id, shared with every other cog that
        # writes prices (see stockimpact.business_lock). Held for the WHOLE
        # read-compute-write of a buy, a sell, a tick or a revenue event on
        # that listing, so two of them can never interleave.
        self.business_locks = stockimpact._BUSINESS_LOCKS
        self.market_tick.start()

    def cog_unload(self):
        self.market_tick.cancel()

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    # Once the web dashboard is live you can retire the Discord trading
    # commands with config.STOCK_DISCORD_COMMANDS_ENABLED = False. Putting the
    # check here rather than on each command means all six are covered at once
    # and none of them can be forgotten.
    #
    # /stock history stays available: it is the staff exploit review, it has no
    # equivalent on the website, and staff shouldn't lose tooling because
    # players moved to a browser.
    # ------------------------------------------------------------------ #
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(config, "STOCK_DISCORD_COMMANDS_ENABLED", True):
            return True

        command = interaction.command.qualified_name if interaction.command else ""
        if command == "stock history":
            return True

        try:
            import webapi

            url = webapi.dashboard_url()
        except ModuleNotFoundError:
            # Dashboard not installed on this host: say so plainly instead of
            # blowing up inside a permission check.
            url = ""
        where = f"\n{url}" if url else ""
        raise app_commands.CheckFailure(
            f"📈 Trading has moved to the web dashboard.{where}\n"
            "Run `/weblogin` to get your sign-in code."
        )

    def business_lock(self, business_id: int):
        return stockimpact.business_lock(business_id)

    # ------------------------------------------------------------------ #
    # Autocomplete: players pick a listed business from a dropdown instead of
    # typing the name (and risking a typo / a delisted business).
    # ------------------------------------------------------------------ #
    async def listed_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every business currently listed on the market, showing live prices."""
        businesses = await self.db.list_businesses(interaction.guild_id)
        current = current.lower()
        choices = []
        for biz_id, name, owner_id, price, prev_price, total, available, delisted in businesses:
            if current and current not in name.lower():
                continue
            label = f"{name} — {fmt_price(price)} ({available:,} available)"
            choices.append(app_commands.Choice(name=label[:100], value=name))
        return choices[:25]

    async def holdings_autocomplete(self, interaction: discord.Interaction, current: str):
        """Only businesses the caller actually holds shares in, so /stock sell
        can't be pointed at something they don't own."""
        rows = await self.db.get_portfolio(interaction.user.id, interaction.guild_id)
        current = current.lower()
        choices = []
        for biz_id, name, shares, cost_basis, price, delisted in rows:
            if delisted or (current and current not in name.lower()):
                continue
            label = f"{name} — you own {shares:,} @ {fmt_price(price)}"
            choices.append(app_commands.Choice(name=label[:100], value=name))
        return choices[:25]

    # ------------------------------------------------------------------ #
    # Helpers shared by buy / sell / stockinfo
    # ------------------------------------------------------------------ #
    async def next_tick_timestamp(self) -> int:
        """When the next market tick (and so the end of the current trading
        window) is due, as a unix timestamp."""
        last_tick = int(await self.db.get_meta("last_market_tick", 0) or 0)
        interval = max(1, int(config.STOCK_TICK_MINUTES)) * 60
        return (last_tick or int(time.time())) + interval

    async def halted_message(self, name: str) -> str:
        resumes = await self.next_tick_timestamp()
        return (
            f"⛔ Trading in **{name}** is halted for the rest of this market period: its price has "
            f"moved more than {config.STOCK_CIRCUIT_BREAKER_PCT * 100:.0f}% since the last tick "
            f"(circuit breaker). Trading reopens at the next market tick, <t:{resumes}:R>."
        )

    # ------------------------------------------------------------------ #
    # Background market simulation.
    #
    # Every tick each listing moves on two things:
    #   1. FUNDAMENTALS — how much revenue the real business behind it earned
    #      since the last tick, priced by stockimpact.tick_move(). A listing
    #      with no business behind it skips this part.
    #   2. NOISE — a small random drift, so even a steady business still has a
    #      market worth watching.
    # A summary is posted to the configured market channel per guild.
    #
    # Each listing is re-priced under its business lock, in one transaction,
    # from a price re-read inside the lock — so a tick can never overwrite a
    # trade that settled a moment earlier (or vice versa). The tick also opens
    # a fresh trading window: the instant-revenue budget refills and any
    # circuit-breaker halt is lifted.
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=1)
    async def market_tick(self):
        # The loop wakes every minute but only moves the market once every
        # STOCK_TICK_MINUTES, measured from a timestamp stored in the database.
        # A free host may restart the bot several times an hour, and a loop that
        # trusted its own clock would fire a fresh tick on every boot — turning
        # a 30-minute market into a slot machine and spamming the market channel.
        now = int(time.time())
        last_tick = int(await self.db.get_meta("last_market_tick", 0) or 0)
        interval = max(1, int(config.STOCK_TICK_MINUTES)) * 60
        if last_tick and now - last_tick < interval:
            return
        await self.db.set_meta("last_market_tick", now)
        if not last_tick:
            return  # first ever boot: start the clock, don't move prices yet

        guild_ids = await self.db.get_all_guild_ids_with_businesses()
        for guild_id in guild_ids:
            businesses = await self.db.list_businesses(guild_id)

            # Which listings are tied to a real registered business. The
            # revenue figure itself is re-read inside the lock below, so a
            # payment landing during the tick is never lost between periods.
            # {business_id: (company_id, company_name, revenue_total, baseline)}
            links = {}
            if config.STOCK_REVENUE_LINK_ENABLED:
                links = {row[0]: row[1:] for row in await self.db.get_revenue_links(guild_id)}

            movers = []
            for biz_id, name, *_ in businesses:
                result = await self._tick_business(guild_id, biz_id, name, links.get(biz_id))
                if result:
                    movers.append(result)

            channel_id = await self.db.get_market_channel(guild_id)
            if not channel_id or not movers:
                continue

            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue

            movers.sort(key=lambda m: pct_change(m[2], m[1]), reverse=True)
            lines = []
            for name, old, new, revenue_delta in movers[:10]:
                change = pct_change(new, old)
                revenue_note = f" · earned {fmt(revenue_delta)}" if revenue_delta is not None else ""
                lines.append(
                    f"{trend_arrow(change)} **{name}** {fmt_price(new)} ({change:+.2f}%){revenue_note}"
                )

            embed = discord.Embed(
                title="📈 Market Update",
                description="\n".join(lines),
                color=discord.Color.dark_gold(),
            )
            if any(m[3] is not None for m in movers):
                embed.set_footer(
                    text="Prices move on what each business actually earned this period."
                )
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    async def _tick_business(self, guild_id: int, biz_id: int, name: str, link):
        """Re-prices ONE listing for the tick. Returns (name, old_price,
        new_price, revenue_delta) or None if the listing vanished."""
        async with self.business_lock(biz_id):
            async with self.db.transaction() as tx:
                state = await self.db.get_business_trade_state(biz_id, tx=tx)
                if not state or state[7]:
                    return None
                (_id, _name, _owner, price, _prev, total, _avail, _delisted,
                 version, _window_open, _budget, _halted, _last_event) = state

                revenue_delta = None
                fundamental = 0.0
                if link:
                    company_id, _company_name, _stale_revenue, baseline = link
                    revenue_total = await self.db.get_company_revenue_total(company_id, tx=tx)
                    # A listing that has never been priced off revenue (just
                    # linked, or upgraded from an older database) only records
                    # where it starts. Otherwise its whole trading history would
                    # land on the price as one enormous single-period beat.
                    if baseline is None:
                        await self.db.set_business_revenue_baseline(biz_id, revenue_total, tx=tx)
                    else:
                        revenue_delta = max(0, revenue_total - int(baseline))
                        fundamental = stockimpact.tick_move(
                            revenue_delta, stockimpact.market_cap(price, total)
                        )
                        await self.db.set_business_revenue_baseline(biz_id, revenue_total, tx=tx)

                drift = random.uniform(-config.STOCK_MAX_RANDOM_DRIFT, config.STOCK_MAX_RANDOM_DRIFT)
                new_price = await self.db.set_business_price(
                    biz_id, price * (1 + fundamental) * (1 + drift), config.STOCK_MIN_PRICE,
                    expected_version=version, tx=tx,
                )
                if new_price is None:
                    raise RuntimeError(f"price_version conflict on business {biz_id} during tick")

                # Record the revenue component in the stock history so players
                # can see in /stockinfo why a stock moved, rather than guessing.
                if abs(fundamental) >= config.STOCK_REVENUE_EVENT_MIN_CHANGE:
                    reason = (
                        f"Earned {fmt(revenue_delta)} this period"
                        if fundamental >= 0
                        else f"Revenue of {fmt(revenue_delta)} missed expectations"
                    )
                    await self.db.log_stock_event(guild_id, biz_id, fundamental * 100, reason, None, tx=tx)

                # New window: refill the instant-revenue budget, lift any halt,
                # and make this price the one the circuit breaker measures from.
                await self.db.open_tick_window(biz_id, tx=tx)

        return (name, price, new_price, revenue_delta)

    @market_tick.before_loop
    async def before_market_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    @app_commands.command(name="market", description="View all businesses currently listed on the stock market.")
    async def market(self, interaction: discord.Interaction):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        businesses = await self.db.list_businesses(interaction.guild_id)
        if not businesses:
            await interaction.response.send_message("No businesses are listed on the market yet.")
            return

        embed = discord.Embed(title="📊 Stock Market", color=discord.Color.dark_gold())
        # Biggest movers first, so trending businesses aren't buried alphabetically.
        for biz_id, name, owner_id, price, prev_price, total, available, delisted in sorted(
            businesses, key=lambda b: pct_change(b[3], b[4]), reverse=True
        ):
            change = pct_change(price, prev_price)
            owner_text = f"<@{owner_id}>" if owner_id else "Publicly held"
            market_cap = price * total
            embed.add_field(
                name=f"{trend_arrow(change)} {name} — {fmt_price(price)} ({change:+.2f}%)",
                value=(
                    f"Owner: {owner_text} | Market cap: {fmt_cap(market_cap)}\n"
                    f"Shares available: {available:,}/{total:,} | ID: `{biz_id}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="topmovers", description="See today's biggest stock gainers and losers.")
    async def topmovers(self, interaction: discord.Interaction):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        businesses = await self.db.list_businesses(interaction.guild_id)
        if not businesses:
            await interaction.response.send_message("No businesses are listed on the market yet.")
            return

        scored = sorted(
            (
                (name, pct_change(price, prev_price), price)
                for _, name, _, price, prev_price, _, _, _ in businesses
            ),
            key=lambda s: s[1],
            reverse=True,
        )
        gainers = [s for s in scored if s[1] > 0][:5]
        losers = [s for s in scored if s[1] < 0][-5:][::-1]

        embed = discord.Embed(title="📈 Top Movers", color=discord.Color.dark_gold())
        embed.add_field(
            name="🔺 Gainers",
            value="\n".join(f"**{n}** {fmt_price(p)} ({c:+.2f}%)" for n, c, p in gainers) or "None",
            inline=True,
        )
        embed.add_field(
            name="🔻 Losers",
            value="\n".join(f"**{n}** {fmt_price(p)} ({c:+.2f}%)" for n, c, p in losers) or "None",
            inline=True,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="stockinfo", description="View detailed info and recent history for one business.")
    @app_commands.describe(business="Pick a listed business")
    @app_commands.autocomplete(business=listed_autocomplete)
    async def stockinfo(self, interaction: discord.Interaction, business: str):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        biz_id, name, owner_id, price, prev_price, total, available, delisted = biz
        change = pct_change(price, prev_price)
        owner_text = f"<@{owner_id}>" if owner_id else "Publicly held"

        embed = discord.Embed(title=f"{name} ({trend_arrow(change)} {change:+.2f}%)", color=discord.Color.dark_gold())
        embed.add_field(name="Current Price", value=fmt_price(price))
        embed.add_field(name="Previous Price", value=fmt_price(prev_price))
        embed.add_field(name="Market Cap", value=fmt_cap(price * total))
        embed.add_field(name="Owner", value=owner_text, inline=False)
        embed.add_field(name="Total Shares", value=f"{total:,}")
        embed.add_field(name="Available on Market", value=f"{available:,}")

        # Circuit breaker status for this trading window.
        state = await self.db.get_business_trade_state(biz_id)
        if state and config.STOCK_CIRCUIT_BREAKER_PCT > 0:
            window_open, halted = state[9], state[11]
            period_move = stockimpact.window_move(price, window_open) * 100
            if halted:
                resumes = await self.next_tick_timestamp()
                embed.add_field(
                    name="⛔ Trading halted",
                    value=(
                        f"Moved {period_move:+.1f}% this period, past the "
                        f"{config.STOCK_CIRCUIT_BREAKER_PCT * 100:.0f}% circuit breaker. "
                        f"Buying and selling reopen at the next market tick, <t:{resumes}:R>."
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="This period",
                    value=(
                        f"{period_move:+.1f}% since the last tick "
                        f"(trading halts at ±{config.STOCK_CIRCUIT_BREAKER_PCT * 100:.0f}%)"
                    ),
                    inline=False,
                )

        # If a real registered business sits behind this listing, show what it
        # earns — that, not luck, is what moves the price.
        link = await self.db.get_company_for_listing(biz_id, interaction.guild_id) \
            if config.STOCK_REVENUE_LINK_ENABLED else None
        if link:
            _company_id, company_name, revenue_total, _explicit = link
            cap = stockimpact.market_cap(price, total)
            embed.add_field(
                name="🏢 Backed by a real business",
                value=(
                    f"**{company_name}** — all-time revenue {fmt(revenue_total or 0)}\n"
                    f"Needs about {fmt(int(stockimpact.expected_revenue(cap)))} of revenue every "
                    f"{config.STOCK_TICK_MINUTES} min to hold this price. "
                    "Earn more and it rises, earn less and it falls."
                ),
                inline=False,
            )

        events = await self.db.get_recent_stock_events(interaction.guild_id, business_id=biz_id, limit=5)
        if events:
            lines = []
            for _, chg, reason, actor_id, ts in events:
                lines.append(f"{chg:+.1f}% — {reason} (<t:{ts}:R>)")
            embed.add_field(name="Recent Events", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="portfolio", description="View your (or someone else's) stock portfolio.")
    @app_commands.describe(user="Whose portfolio to view (defaults to you)")
    async def portfolio(self, interaction: discord.Interaction, user: discord.Member = None):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        target = user or interaction.user
        rows = await self.db.get_portfolio(target.id, interaction.guild_id)
        if not rows:
            await interaction.response.send_message(f"{target.display_name} doesn't hold any stock.")
            return

        embed = discord.Embed(title=f"{target.display_name}'s Portfolio", color=discord.Color.blurple())
        total_value = 0
        total_invested = 0
        for biz_id, name, shares, cost_basis, price, delisted in rows:
            value = shares * price
            total_value += value
            total_invested += cost_basis
            gain = value - cost_basis
            gain_pct = (gain / cost_basis * 100) if cost_basis else 0
            status = " (delisted)" if delisted else ""
            embed.add_field(
                name=f"{name}{status}",
                value=(
                    f"{shares:,} shares @ {fmt_price(price)} = {fmt_price(value)}\n"
                    f"Invested: {fmt(int(cost_basis))} | P/L: {fmt_price(gain)} ({gain_pct:+.1f}%)"
                ),
                inline=False,
            )

        overall_gain = total_value - total_invested
        embed.add_field(
            name="Total Portfolio Value",
            value=f"{fmt_price(total_value)} (P/L: {fmt_price(overall_gain)})",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    stock_group = app_commands.Group(name="stock", description="Buy and sell shares on the stock market.")

    # ------------------------------------------------------------------ #
    # Every buy and sell runs as: take the listing's lock -> open ONE database
    # transaction -> re-read the listing fresh -> validate -> spend / move
    # shares / re-price with conditional statements -> commit. Any refusal
    # raises OrderRejected, which rolls the whole thing back — including the
    # cooldown claim — so a refused order costs the player nothing.
    # ------------------------------------------------------------------ #
    @stock_group.command(name="buy", description="Buy shares of a business.")
    @app_commands.describe(business="Pick a listed business", shares="Number of shares to buy (e.g. 50, 10k)")
    @app_commands.autocomplete(business=listed_autocomplete)
    async def buy(self, interaction: discord.Interaction, business: str, shares: str):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        uid, gid = interaction.user.id, interaction.guild_id
        biz = await self.db.get_business_by_name(business, gid)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        biz_id, name, owner_id, price, prev_price, total, available, delisted = biz

        # Shorthand: "10k" means 10,000 shares. "all" fills as much of the
        # available float as the per-order cap allows. Resolved BEFORE the
        # lock so a typo never touches the market; everything it depends on
        # is re-checked inside.
        max_trade = max(1, int(total * config.STOCK_MAX_TRADE_PCT_OF_SHARES))
        shares, error = parse_count(
            shares, minimum=1, maximum=1_000_000_000,
            available=min(available, max_trade), noun="number of shares",
        )
        if error:
            await send_error(interaction, error)
            return

        try:
            filled = await self._execute_buy(uid, gid, biz_id, shares)
        except OrderRejected as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        halt_note = (
            "\n⛔ This move tripped the circuit breaker — trading in this stock is halted until the next market tick."
            if filled["halted"] else ""
        )
        await interaction.response.send_message(
            f"Bought **{filled['shares']:,}** share(s) of **{filled['name']}** for **{fmt(filled['cost'])}** "
            f"(avg {fmt_price(filled['exec_price'])}/share).\n"
            f"New price: {fmt_price(filled['new_price'])}{halt_note}"
        )

    async def _execute_buy(self, uid: int, gid: int, biz_id: int, shares: int) -> dict:
        now = int(time.time())
        async with self.business_lock(biz_id):
            async with self.db.transaction() as tx:
                # Fresh read INSIDE the lock — never trust a price or a float
                # fetched before we owned the listing.
                state = await self.db.get_business_trade_state(biz_id, tx=tx)
                if not state or state[7]:
                    raise OrderRejected("No listed business by that name.", "not_listed")
                (_id, name, _owner, price, _prev, total, available, _delisted,
                 version, window_open, _budget, halted, _last_event) = state

                if halted:
                    raise OrderRejected(
                        await self.halted_message(name), "halted",
                        resumes_at=await self.next_tick_timestamp(),
                    )

                if shares > available:
                    raise OrderRejected(
                        f"Only {available:,} shares of {name} are available on the market.",
                        "insufficient_float", available=available,
                    )

                max_trade = max(1, int(total * config.STOCK_MAX_TRADE_PCT_OF_SHARES))
                if shares > max_trade:
                    raise OrderRejected(
                        f"To prevent market manipulation, a single order is capped at {max_trade:,} shares of {name} "
                        f"({config.STOCK_MAX_TRADE_PCT_OF_SHARES * 100:.0f}% of total shares). Split large orders across "
                        "multiple trades.",
                        "order_cap", max_shares=max_trade,
                    )

                # ---- rolling volume cap (config.STOCK_BUY_DAILY_VOLUME_PCT_OF_SHARES)
                # The per-order cap limits one order; this limits how much of a
                # stock one player can accumulate inside a window, so a chain
                # of legal-sized orders can't build a huge position before the
                # price has caught up.
                volume_pct = float(config.STOCK_BUY_DAILY_VOLUME_PCT_OF_SHARES or 0)
                if volume_pct > 0:
                    window_secs = int(config.STOCK_BUY_VOLUME_WINDOW_HOURS * 3600)
                    volume_cap = max(max_trade, int(total * volume_pct))
                    bought = await self.db.get_user_buy_volume(uid, gid, biz_id, now - window_secs, tx=tx)
                    if bought + shares > volume_cap:
                        room = max(0, volume_cap - bought)
                        raise OrderRejected(
                            f"You've bought {bought:,} shares of {name} in the last "
                            f"{config.STOCK_BUY_VOLUME_WINDOW_HOURS:g}h; the limit is {volume_cap:,} "
                            f"({volume_pct * 100:.0f}% of total shares) per player per period. "
                            + (f"You can still buy up to {room:,} right now." if room else "Try again later."),
                            "volume_cap", max_shares=room, bought=bought, cap=volume_cap,
                        )

                # ---- buy cooldown (config.STOCK_BUY_COOLDOWN_MINUTES) -------
                # Claimed atomically like the sell cooldown; rolled back with
                # everything else if the order is refused below.
                cooldown_secs = int(config.STOCK_BUY_COOLDOWN_MINUTES * 60)
                if not await self.db.try_consume_stock_buy_cooldown(uid, gid, now, cooldown_secs, tx=tx):
                    previous_buy = await self.db.get_last_stock_buy(uid, gid, tx=tx)
                    remaining = max(1, cooldown_secs - (now - previous_buy))
                    raise OrderRejected(
                        f"\u23f3 You've just bought shares. You can buy again in {format_duration(remaining)}.",
                        "cooldown", retry_after=remaining,
                    )

                # The order is settled at the MIDPOINT of the price move it causes, not
                # at the stale pre-trade price. Paying the old price and then pumping the
                # price made buy -> sell round trips structurally profitable (an infinite
                # money loop); charging the mid means a round trip is always a loss.
                impact = config.STOCK_TRADE_IMPACT_FACTOR * (shares / total)
                exec_price = price * (1 + impact / 2)
                cost = round(shares * exec_price)
                if not await self.db.try_spend_cash(uid, gid, cost, tx=tx):
                    cash = await self.db.get_cash(uid, gid, tx=tx)
                    raise OrderRejected(
                        f"You need {fmt(cost)} in cash to buy {shares:,} share(s) of {name}. You have {fmt(cash)}.",
                        "insufficient_funds", required=cost, cash=cash,
                    )

                # Take the shares off the market with the floor/ceiling guard;
                # if it refuses, the cash charge above is rolled back with it.
                if not await self.db.adjust_available_shares(biz_id, -shares, tx=tx):
                    raise OrderRejected(
                        f"Only {available:,} shares of {name} are available on the market.",
                        "insufficient_float", available=available,
                    )

                await self.db.upsert_holding(uid, gid, biz_id, shares, cost, tx=tx)
                await self.db.log_transaction(uid, gid, "stock_buy", -cost, f"Bought {shares} sh. {name}", tx=tx)
                await self.db.record_stock_trade(
                    uid, gid, biz_id, "buy", shares, cost, price, at_cap=(shares >= max_trade), tx=tx
                )

                new_price = await self.db.set_business_price(
                    biz_id, price * (1 + impact), config.STOCK_MIN_PRICE, expected_version=version, tx=tx
                )
                if new_price is None:
                    raise RuntimeError(f"price_version conflict on business {biz_id} during buy")

                tripped = await stockimpact.check_circuit_breaker(
                    self.db, gid, biz_id, new_price, window_open, bool(halted), "trading", uid, tx=tx
                )

        return {
            "name": name, "shares": shares, "cost": cost, "exec_price": exec_price,
            "new_price": new_price, "halted": tripped,
        }

    @stock_group.command(name="sell", description="Sell shares of a business you hold.")
    @app_commands.describe(business="Pick a business you hold shares in", shares="Number of shares to sell (e.g. 50, 10k) or 'all'")
    @app_commands.autocomplete(business=holdings_autocomplete)
    async def sell(self, interaction: discord.Interaction, business: str, shares: str):
        # Stock commands are restricted to the server's market channel(s)
        # (config.STOCK_CHANNEL_IDS / `/eco-admin channels`). Unrestricted
        # until one is set.
        if not await channels.enforce(interaction, "stock"):
            return

        uid, gid = interaction.user.id, interaction.guild_id
        biz = await self.db.get_business_by_name(business, gid)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        # Shorthand: "10k" means 10,000 shares, "all" means the whole holding
        # (capped by the per-order limit). Resolved BEFORE the cooldown is
        # claimed so a typo never burns the cooldown.
        held = await self.db.get_holding(uid, gid, biz[0])
        owned_now = held[0] if held else 0
        per_order_cap = max(1, int(biz[5] * config.STOCK_MAX_TRADE_PCT_OF_SHARES))
        shares, error = parse_count(
            shares, minimum=1, maximum=1_000_000_000,
            available=min(owned_now, per_order_cap), noun="number of shares",
        )
        if error:
            await send_error(interaction, error)
            return

        try:
            filled = await self._execute_sell(uid, gid, biz[0], shares)
        except OrderRejected as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        cooldown_secs = int(config.STOCK_SELL_COOLDOWN_MINUTES * 60)
        cooldown_note = (
            f"\nNext sell available in {format_duration(cooldown_secs)}." if cooldown_secs > 0 else ""
        )
        halt_note = (
            "\n⛔ This move tripped the circuit breaker — trading in this stock is halted until the next market tick."
            if filled["halted"] else ""
        )
        await interaction.response.send_message(
            f"Sold **{filled['shares']:,}** share(s) of **{filled['name']}** for **{fmt(filled['proceeds'])}** "
            f"(avg {fmt_price(filled['exec_price'])}/share).\n"
            f"New price: {fmt_price(filled['new_price'])}{cooldown_note}{halt_note}"
        )

    async def _execute_sell(self, uid: int, gid: int, biz_id: int, shares: int) -> dict:
        now = int(time.time())
        async with self.business_lock(biz_id):
            async with self.db.transaction() as tx:
                state = await self.db.get_business_trade_state(biz_id, tx=tx)
                if not state or state[7]:
                    raise OrderRejected("No listed business by that name.", "not_listed")
                (_id, name, _owner, price, _prev, total, _available, _delisted,
                 version, window_open, _budget, halted, last_revenue_event) = state

                if halted:
                    raise OrderRejected(
                        await self.halted_message(name), "halted",
                        resumes_at=await self.next_tick_timestamp(),
                    )

                # ---- insider lock-out (config.STOCK_INSIDER_SELL_LOCKOUT_MINUTES)
                # The owner, the linked company's owner / withdraw-capable
                # staff, and any large holder can't sell into a price spike
                # that a payment to the business just caused.
                lockout_secs = int(config.STOCK_INSIDER_SELL_LOCKOUT_MINUTES * 60)
                if lockout_secs > 0 and last_revenue_event and now - last_revenue_event < lockout_secs:
                    if await self.db.is_stock_insider(
                        uid, gid, biz_id, config.STOCK_INSIDER_HOLDER_THRESHOLD,
                        config.COMPANY_WITHDRAW_POSITIONS, tx=tx,
                    ):
                        remaining = lockout_secs - (now - last_revenue_event)
                        raise OrderRejected(
                            f"🔒 **{name}**'s price just moved on revenue paid to the business, and as an insider "
                            f"(owner, company staff or a holder of {config.STOCK_INSIDER_HOLDER_THRESHOLD * 100:.0f}%+ "
                            f"of the shares) you can't sell for {format_duration(remaining)} after that.",
                            "insider_lockout", retry_after=remaining,
                        )

                # ---- sell cooldown (config.STOCK_SELL_COOLDOWN_MINUTES) ----
                # Claimed atomically before anything is sold, so a burst of
                # orders can't all get through on the same stale timestamp.
                # Refusing the order below rolls the claim back automatically.
                cooldown_secs = int(config.STOCK_SELL_COOLDOWN_MINUTES * 60)
                if not await self.db.try_consume_stock_sell_cooldown(uid, gid, now, cooldown_secs, tx=tx):
                    previous_sell = await self.db.get_last_stock_sell(uid, gid, tx=tx)
                    remaining = max(1, cooldown_secs - (now - previous_sell))
                    raise OrderRejected(
                        f"\u23f3 You've already sold recently. You can sell again in "
                        f"{format_duration(remaining)}.",
                        "cooldown", retry_after=remaining,
                    )

                holding = await self.db.get_holding(uid, gid, biz_id, tx=tx)
                if not holding or holding[0] < shares:
                    owned = holding[0] if holding else 0
                    raise OrderRejected(
                        f"You only own {owned:,} share(s) of {name}.",
                        "insufficient_shares", owned=owned,
                    )

                max_trade = max(1, int(total * config.STOCK_MAX_TRADE_PCT_OF_SHARES))
                if shares > max_trade:
                    raise OrderRejected(
                        f"To prevent market manipulation, a single order is capped at {max_trade:,} shares of {name} "
                        f"({config.STOCK_MAX_TRADE_PCT_OF_SHARES * 100:.0f}% of total shares). Split large orders across "
                        "multiple trades.",
                        "order_cap", max_shares=max_trade,
                    )

                owned_shares, cost_basis = holding
                avg_cost = cost_basis / owned_shares if owned_shares else 0
                cost_delta = -round(avg_cost * shares)

                # Settle at the midpoint of the downward pressure this sale creates, for
                # the same reason as /stock buy above.
                impact = config.STOCK_TRADE_IMPACT_FACTOR * (shares / total)
                exec_price = price * (1 - impact / 2)
                proceeds = round(shares * exec_price)

                # Remove the shares FIRST with a conditional UPDATE, and only pay out if
                # that claim actually won. Reading the holding and then writing it back
                # let several simultaneous /stock sell commands each sell the same
                # shares and each get paid.
                if not await self.db.try_sell_shares(uid, gid, biz_id, shares, cost_delta, tx=tx):
                    raise OrderRejected(
                        f"You no longer own {shares:,} share(s) of {name} — check `/portfolio` and try again.",
                        "insufficient_shares",
                    )

                await self.db.add_cash(uid, gid, proceeds, tx=tx)
                if not await self.db.adjust_available_shares(biz_id, shares, tx=tx):
                    # Would put more shares on the market than exist — refuse
                    # and roll everything (including the payout) back.
                    raise OrderRejected(
                        f"The market can't take {shares:,} more share(s) of {name} right now. Try again after the next tick.",
                        "float_full",
                    )
                await self.db.log_transaction(uid, gid, "stock_sell", proceeds, f"Sold {shares} sh. {name}", tx=tx)
                await self.db.record_stock_trade(
                    uid, gid, biz_id, "sell", shares, proceeds, price, at_cap=(shares >= max_trade), tx=tx
                )

                new_price = await self.db.set_business_price(
                    biz_id, price * (1 - impact), config.STOCK_MIN_PRICE, expected_version=version, tx=tx
                )
                if new_price is None:
                    raise RuntimeError(f"price_version conflict on business {biz_id} during sell")

                tripped = await stockimpact.check_circuit_breaker(
                    self.db, gid, biz_id, new_price, window_open, bool(halted), "trading", uid, tx=tx
                )

        return {
            "name": name, "shares": shares, "proceeds": proceeds, "exec_price": exec_price,
            "new_price": new_price, "halted": tripped,
        }

    # ------------------------------------------------------------------ #
    # /stock history <player> — staff-only exploit check on one player.
    #
    # Deliberately readable top to bottom: a one-line verdict, then the money,
    # then anything that looks engineered, then the raw orders. Staff should be
    # able to answer "is this player cheating?" from the first two lines.
    # ------------------------------------------------------------------ #
    @stock_group.command(
        name="history",
        description="[Staff] Review a player's whole trading history and flag possible stock exploits.",
    )
    @app_commands.describe(
        player="The player to review",
        orders="How many individual orders to list (5-25, default 10)",
    )
    async def history(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        orders: app_commands.Range[int, 5, 25] = 10,
    ):
        from cogs.admin import is_eco_staff

        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await send_error(interaction, "This command can only be used in a server.")
            return
        if not is_eco_staff(interaction.user, interaction.guild):
            await send_error(
                interaction,
                "Only economy staff can review another player's trading history. "
                "Use `/portfolio` to see holdings instead.",
            )
            return

        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id

        trades = await self.db.get_user_stock_trades(player.id, gid, limit=500)
        if not trades:
            await interaction.followup.send(
                f"**{player.display_name}** has never traded on the stock market.", ephemeral=True
            )
            return

        # Everything the analysis needs, fetched once.
        listings = await self.db.list_businesses(gid, include_delisted=True)
        businesses = {}
        for biz_id, name, owner_id, price, *_ in listings:
            businesses[biz_id] = {
                "name": name,
                "owner_id": owner_id,
                "price": price,
                "is_own": owner_id == player.id,
            }

        holdings = {
            row[0]: row[2] for row in await self.db.get_portfolio(player.id, gid)
        }

        oldest = min(t[8] for t in trades)
        events = await self.db.get_stock_events_since(gid, oldest)

        report = stockhistory.analyse(trades, events, businesses, holdings)
        for embed in build_history_embeds(player, report, orders):
            await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# Rendering for /stock history. Kept out of the command so the layout is easy
# to adjust, and returns a list because a busy trader's report can outgrow the
# 6000-character limit on a single embed.
# --------------------------------------------------------------------------- #
def build_history_embeds(player, report, order_limit: int):
    summary = report["summary"]
    verdict = report["verdict"]
    findings = report["findings"]

    colour = {
        stockhistory.HIGH: discord.Color.red(),
        stockhistory.MEDIUM: discord.Color.orange(),
        stockhistory.LOW: discord.Color.gold(),
    }.get(report["worst"], discord.Color.green())

    main = discord.Embed(
        title=f"\U0001f4c8 Trading review \u2014 {player.display_name}",
        description=f"## {verdict['emoji']} {verdict['title']}\n{verdict['text']}",
        color=colour,
    )
    if player.display_avatar:
        main.set_thumbnail(url=player.display_avatar.url)

    profit = summary["realised"]
    profit_sign = "+" if profit >= 0 else "\u2212"
    profit_line = f"**{profit_sign}{fmt(abs(profit))}**"
    main.add_field(
        name="The money",
        value=(
            f"Spent on shares: {fmt(summary['spent'])}\n"
            f"Made selling: {fmt(summary['received'])}\n"
            f"Profit taken: {profit_line}"
        ),
        inline=True,
    )
    main.add_field(
        name="The activity",
        value=(
            f"{summary['orders']:,} orders "
            f"({summary['buys']:,} buy / {summary['sells']:,} sell)\n"
            f"{summary['stocks_traded']} different stocks\n"
            f"First trade <t:{summary['first_ts']}:R>"
        ),
        inline=True,
    )

    if summary["still_held"]:
        held = "\n".join(
            f"{h['shares']:,} \u00d7 {h['name']} \u2014 {fmt(h['value'])}"
            for h in summary["still_held"][:6]
        )
        if len(summary["still_held"]) > 6:
            held += f"\n*+{len(summary['still_held']) - 6} more*"
        main.add_field(name="Still holding", value=held, inline=False)

    if findings:
        for finding in findings[:5]:
            emoji = stockhistory.SEVERITY_EMOJI[finding["severity"]]
            label = stockhistory.SEVERITY_NAME[finding["severity"]]
            main.add_field(
                name=f"{emoji} {finding['title']} \u2014 {label}",
                value=f"{finding['detail']}\n*{finding['context']}*\n<t:{finding['ts']}:f>"[:1024],
                inline=False,
            )
        if len(findings) > 5:
            main.add_field(
                name="\u2026and more",
                value=f"{len(findings) - 5} further flags of the same kind were found.",
                inline=False,
            )
    else:
        main.add_field(
            name="\U0001f7e2 Nothing flagged",
            value=(
                "No trades landed suspiciously close to a price move, no instant flips, "
                "and no repeated maximum-size orders."
            ),
            inline=False,
        )

    main.set_footer(text="Only staff can see this. /transactions shows their cash movements.")
    embeds = [main]

    # Second embed: the raw orders, newest first.
    lines = []
    for _, biz_id, name, side, shares, amount, price, at_cap, ts in report["trades"][:order_limit]:
        marker = "\U0001f7e2 BUY " if side == "buy" else "\U0001f534 SELL"
        cap = " `MAX SIZE`" if at_cap else ""
        lines.append(
            f"{marker} **{shares:,}** {name} @ ${price:,.2f} = {fmt(amount)}{cap} \u00b7 <t:{ts}:R>"
        )

    # Embed descriptions cap at 4096 characters; drop the oldest orders rather
    # than letting a long business name make the whole command fail.
    description = "\n".join(lines) if lines else "No orders."
    while len(description) > 3900 and len(lines) > 1:
        lines.pop()
        description = "\n".join(lines) + "\n*(older orders trimmed to fit)*"

    orders_embed = discord.Embed(
        title=f"Recent orders \u2014 {player.display_name}",
        description=description,
        color=colour,
    )
    shown = min(order_limit, len(report["trades"]))
    orders_embed.set_footer(
        text=f"Showing the {shown} newest of {len(report['trades'])} recorded orders."
    )
    embeds.append(orders_embed)
    return embeds


async def setup(bot: commands.Bot):
    await bot.add_cog(Stocks(bot))
