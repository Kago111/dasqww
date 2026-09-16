"""
Player-facing commands for real businesses (companies) — separate from the
stock market. Companies are registered by admins with /eco-admin company
create; this cog lets players pay them, owners and staff run the business
account, owners build a staff roster (COO, managers, employees...), and owners
settle the business tax with /tax pay.

Two rules worth remembering when reading this file:

* Money a company EARNS (/paycompany, /eco-admin company addrevenue) counts as
  revenue and is taxed.
* Money the owner PUTS IN (/company deposit) is their own already-taxed cash.
  It is not revenue, it is never taxed, and it never moves a stock price.
"""

import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import channels
import config
import stockimpact
from amounts import parse_amount
from cogs.economy import fmt, format_duration, send_error

OWNER_RANK = -1  # the owner outranks every job title


def tax_owed(revenue_total: int, tax_paid: int) -> int:
    """Outstanding tax = the tax rate applied to all-time revenue, minus what
    has already been paid. Owner deposits are not revenue, so they never
    increase this."""
    return max(0, round(revenue_total * config.BUSINESS_TAX_RATE) - tax_paid)


def position_rank(position: Optional[str]) -> Optional[int]:
    """Seniority index of a job title — lower is more senior. None if the title
    isn't in config.COMPANY_POSITIONS (e.g. it was renamed in the config after
    someone was hired)."""
    if position is None:
        return None
    for index, name in enumerate(config.COMPANY_POSITIONS):
        if name.lower() == position.lower():
            return index
    return None


def is_eco_staff_member(interaction: discord.Interaction) -> bool:
    """Economy staff can administer any company. Imported lazily so this cog
    and the admin cog can't deadlock on each other at import time."""
    from cogs.admin import is_eco_staff

    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    return is_eco_staff(interaction.user, interaction.guild)


class CompanyAccess:
    """Who the caller is at a given company, and what that lets them do."""

    def __init__(self, company, rank: Optional[int], position: Optional[str], is_admin: bool):
        self.company = company
        self.rank = rank              # OWNER_RANK, a position index, or None
        self.position = position      # job title, or None for owner/admin
        self.is_admin = is_admin

    @property
    def is_owner(self) -> bool:
        return self.rank == OWNER_RANK and not self.is_admin

    @property
    def title(self) -> str:
        if self.is_admin:
            return "Economy staff"
        if self.rank == OWNER_RANK:
            return "Owner"
        return self.position or "Employee"

    def outranks(self, other_rank: Optional[int]) -> bool:
        """True if the caller may act on someone at `other_rank`. Admins and the
        owner may act on anyone; staff only on ranks strictly below their own."""
        if self.is_admin or self.rank == OWNER_RANK:
            return True
        if self.rank is None or other_rank is None:
            return False
        return self.rank < other_rank

    def may(self, allowed_positions) -> bool:
        """True if this caller holds a position on the given config list."""
        if self.is_admin or self.rank == OWNER_RANK:
            return True
        if not self.position:
            return False
        return any(p.lower() == self.position.lower() for p in allowed_positions)


class Companies(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    # Autocomplete
    # ------------------------------------------------------------------ #
    async def company_autocomplete(self, interaction: discord.Interaction, current: str):
        """Every registered company on the server."""
        companies = await self.db.list_companies(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=name[:100], value=name)
            for _, name, *_ in companies
            if current in name.lower()
        ][:25]

    async def my_company_autocomplete(self, interaction: discord.Interaction, current: str):
        """Only companies the caller owns or works at (economy staff see all)."""
        gid = interaction.guild_id
        if is_eco_staff_member(interaction):
            return await self.company_autocomplete(interaction, current)

        owned = await self.db.get_companies_by_owner(interaction.user.id, gid)
        employed = await self.db.get_companies_for_staff(interaction.user.id, gid)
        current = current.lower()
        seen, choices = set(), []
        for row in owned:
            name = row[1]
            seen.add(name.lower())
            if current in name.lower():
                choices.append(app_commands.Choice(name=f"{name} — Owner"[:100], value=name))
        for _, name, position in employed:
            if name.lower() in seen or current not in name.lower():
                continue
            choices.append(app_commands.Choice(name=f"{name} — {position}"[:100], value=name))
        return choices[:25]

    # ------------------------------------------------------------------ #
    # Access helper
    # ------------------------------------------------------------------ #
    async def get_access(self, interaction: discord.Interaction, business: str) -> Optional[CompanyAccess]:
        """Looks up the company and the caller's standing at it. Sends the error
        itself and returns None if the company doesn't exist or the caller has
        nothing to do with it."""
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await send_error(interaction, "No business by that name.")
            return None

        company_id, name, owner_id, *_ = company
        if owner_id == interaction.user.id:
            return CompanyAccess(company, OWNER_RANK, None, False)

        position = await self.db.get_company_staff_position(company_id, interaction.user.id)
        if position:
            return CompanyAccess(company, position_rank(position), position, False)

        if is_eco_staff_member(interaction):
            return CompanyAccess(company, OWNER_RANK, None, True)

        await send_error(interaction, f"You don't own or work at **{name}**.")
        return None

    # ------------------------------------------------------------------ #
    @app_commands.command(name="paycompany", description="Pay a business for goods or services.")
    @app_commands.describe(business="The business name", amount="Amount to pay (e.g. 500, 10k, 1.5m)")
    @app_commands.autocomplete(business=company_autocomplete)
    async def paycompany(self, interaction: discord.Interaction, business: str, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        company = await self.db.get_company_by_name(business, gid)
        if not company:
            await send_error(interaction, "No business by that name.")
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company

        # Paying a company you can also take money out of just cycles money
        # through an account you control — combined with /company withdraw that
        # was a laundering loop, so it's blocked for the owner and for any
        # employee who holds withdrawal rights.
        if owner_id == uid:
            await send_error(interaction, "You can't pay your own business.")
            return
        position = await self.db.get_company_staff_position(company_id, uid)
        if position and any(p.lower() == position.lower() for p in config.COMPANY_WITHDRAW_POSITIONS):
            await send_error(
                interaction,
                f"You can withdraw from **{name}**'s account, so you can't pay into it as a customer. "
                "Use `/company deposit` instead.",
            )
            return

        cash, _ = await self.db.get_balance(uid, gid)
        amt, error = parse_amount(amount, available=cash, noun="payment")
        if error:
            await send_error(interaction, error)
            return

        # ---- per-business payment cooldown (config.PAYCOMPANY_COOLDOWN_MINUTES)
        # Claimed atomically BEFORE any money moves, same as the /stock sell
        # cooldown, so a burst of payments can't all pass on one stale
        # timestamp. Without it one account could pay the same business ten
        # times in ten seconds and stack ten "instant" price nudges into one
        # big pump. Handed back if the payment fails further down.
        now = int(time.time())
        cooldown_secs = int(config.PAYCOMPANY_COOLDOWN_MINUTES * 60)
        cooldown_key = f"paycompany:{company_id}"
        previous_payment = await self.db.get_keyed_cooldown(uid, gid, cooldown_key)
        if not await self.db.try_consume_keyed_cooldown(uid, gid, cooldown_key, now, cooldown_secs):
            remaining = max(1, cooldown_secs - (now - previous_payment))
            await send_error(
                interaction,
                f"\u23f3 You paid **{name}** recently. You can pay them again in {format_duration(remaining)}.",
            )
            return

        reason = f"Payment from {interaction.user.display_name}"
        new_tax = tax_owed(revenue + amt, tax_paid)

        # Trade drives a business's share price: if this business is listed on
        # the stock market, being paid nudges its stock up straight away, and
        # the market tick then prices the whole period's revenue properly.
        # charge_payer + credit_revenue: the cash leaves the payer, lands in
        # the business and moves the stock in ONE locked transaction — so a
        # crash can never leave the money charged but not credited.
        try:
            impact = await stockimpact.apply_revenue_payment(
                self.db, gid, company_id, amt, reason, uid, credit_revenue=True, charge_payer=True
            )
        except stockimpact.InsufficientCash:
            await self.db.restore_keyed_cooldown(uid, gid, cooldown_key, previous_payment)
            await send_error(interaction, f"You need {fmt(amt)} in cash but only have {fmt(cash)}.")
            return
        stock_line = "\n" + stockimpact.describe(*impact) if impact else ""
        # A neutral transfer (cash -> business account) for the payer's history
        # and for the reconciliation audit, which needs to know who paid whom.
        await self.db.log_transaction(uid, gid, "company_payment", -amt, f"Paid {name}")

        await interaction.response.send_message(
            f"💰 Paid **{fmt(amt)}** to **{name}**.\n"
            f"Business all-time revenue: {fmt(revenue + amt)} | Outstanding tax: {fmt(new_tax)}"
            f"{stock_line}"
        )

    # ------------------------------------------------------------------ #
    company_group = app_commands.Group(name="company", description="Manage your business.")

    @company_group.command(name="balance", description="View your business account balance and tax bill.")
    @app_commands.describe(business="The business name")
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_balance(self, interaction: discord.Interaction, business: str):
        access = await self.get_access(interaction, business)
        if not access:
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = access.company
        owed = tax_owed(revenue, tax_paid)
        deposits = await self.db.get_company_deposits(company_id)
        staff_count = await self.db.count_company_staff(company_id)

        embed = discord.Embed(title=f"🏢 {name}", color=discord.Color.dark_teal())
        embed.add_field(name="Account Balance", value=fmt(balance), inline=True)
        embed.add_field(name="Withdrawable", value=fmt(max(0, balance - owed)), inline=True)
        embed.add_field(name="Your Role", value=access.title, inline=True)
        embed.add_field(name="All-Time Revenue", value=fmt(revenue), inline=True)
        embed.add_field(name="Owner Deposits (untaxed)", value=fmt(deposits), inline=True)
        embed.add_field(name="Employees", value=f"{staff_count}", inline=True)
        embed.add_field(
            name=f"Tax ({config.BUSINESS_TAX_RATE * 100:.0f}% of revenue)",
            value=f"Paid {fmt(tax_paid)} | Outstanding **{fmt(owed)}**"
            + ("" if owed == 0 else " — pay with `/tax pay`"),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    # Public business information — any player can look these up, but only in
    # the channel(s) set aside for it (config.BUSINESS_INFO_CHANNEL_IDS, or
    # `/eco-admin channels add area:business`).
    # ------------------------------------------------------------------ #
    @company_group.command(name="list", description="See every business on the server, its owner and its revenue.")
    async def company_list(self, interaction: discord.Interaction):
        if not await channels.enforce(interaction, "business"):
            return

        companies = await self.db.list_companies(interaction.guild_id)
        if not companies:
            await interaction.response.send_message("No businesses are registered on this server yet.")
            return

        # Busiest businesses first, so the board reads like a ranking.
        companies = sorted(companies, key=lambda c: c[4], reverse=True)
        shown = companies[:25]

        embed = discord.Embed(
            title="🏢 Registered Businesses",
            description=(
                f"{len(companies)} business(es) registered. "
                "Use `/company info` for the full picture of any one of them."
            ),
            color=discord.Color.dark_teal(),
        )
        for company_id, name, owner_id, balance, revenue, tax_paid, created_at in shown:
            owner_member = interaction.guild.get_member(owner_id) if owner_id else None
            # Plain display names, never mentions — a public listing should not
            # ping every business owner on the server.
            owner_name = (
                owner_member.display_name if owner_member
                else (f"Unknown member ({owner_id})" if owner_id else "Ownerless")
            )
            owed = tax_owed(revenue, tax_paid)
            staff_count = await self.db.count_company_staff(company_id)
            value = f"Owner: {owner_name} | Staff: {staff_count}\nAll-time revenue: {fmt(revenue)}"
            if config.COMPANY_PUBLIC_INFO_SHOW_BALANCE:
                value += f" | Account: {fmt(balance)}"
            value += f"\nOutstanding tax: {fmt(owed)}"
            embed.add_field(name=name, value=value, inline=False)

        if len(companies) > len(shown):
            embed.set_footer(text=f"Showing the {len(shown)} highest-earning of {len(companies)} businesses.")
        await interaction.response.send_message(embed=embed)

    @company_group.command(name="info", description="Look up any business: owner, staff, revenue and tax.")
    @app_commands.describe(business="The business name")
    @app_commands.autocomplete(business=company_autocomplete)
    async def company_info(self, interaction: discord.Interaction, business: str):
        if not await channels.enforce(interaction, "business"):
            return

        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await send_error(interaction, "No business by that name.")
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        owed = tax_owed(revenue, tax_paid)
        deposits = await self.db.get_company_deposits(company_id)
        staff = await self.db.get_company_staff(company_id)

        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        owner_name = (
            owner_member.display_name if owner_member
            else (f"Unknown member ({owner_id})" if owner_id else "Ownerless")
        )

        embed = discord.Embed(title=f"🏢 {name}", color=discord.Color.dark_teal())
        embed.add_field(name="Owner", value=owner_name, inline=True)
        embed.add_field(name="Employees", value=f"{len(staff)}", inline=True)
        if created_at:
            embed.add_field(name="Registered", value=f"<t:{created_at}:R>", inline=True)
        embed.add_field(name="All-Time Revenue", value=fmt(revenue), inline=True)
        if config.COMPANY_PUBLIC_INFO_SHOW_BALANCE:
            embed.add_field(name="Account Balance", value=fmt(balance), inline=True)
            # Owner deposits are personal money paid in — never revenue, never
            # taxed — so they are always shown apart from what was earned.
            embed.add_field(name="Owner Deposits (untaxed)", value=fmt(deposits), inline=True)
        embed.add_field(
            name=f"Tax ({config.BUSINESS_TAX_RATE * 100:.0f}% of revenue)",
            value=f"Paid {fmt(tax_paid)} | Outstanding **{fmt(owed)}**",
            inline=False,
        )

        if staff:
            ordered = sorted(
                staff,
                key=lambda row: (position_rank(row[1]) if position_rank(row[1]) is not None else 99, row[2] or 0),
            )
            lines = []
            for user_id, position, _hired_at in ordered:
                member = interaction.guild.get_member(user_id)
                display = member.display_name if member else f"Unknown member ({user_id})"
                lines.append(f"**{position}** · {display}")
            embed.add_field(name=f"Staff ({len(staff)})", value="\n".join(lines)[:1024], inline=False)

        # If a stock market listing shares the business's name, show where it is
        # trading. The two systems are separate, but players think of them as
        # the same company.
        listing = await self.db.get_listing_for_company(company_id, interaction.guild_id) \
            if config.STOCK_REVENUE_LINK_ENABLED else None
        if listing:
            business_id, listing_name, price, total = listing
            expected = stockimpact.expected_revenue(stockimpact.market_cap(price, total))
            embed.add_field(
                name="📈 On the stock market",
                value=(
                    f"**{listing_name}** — ${price:,.2f}/share across {total:,} shares.\n"
                    f"Its price follows this business's revenue: about {fmt(int(expected))} every "
                    f"{config.STOCK_TICK_MINUTES} minutes holds the price, more sends it up, less "
                    "sends it down."
                ),
                inline=False,
            )
        else:
            listing = await self.db.get_business_by_name(name, interaction.guild_id)
            if listing:
                _, _, _, price, prev_price, total, available, _ = listing
                embed.add_field(
                    name="On the stock market",
                    value=f"${price:,.2f}/share · {available:,} of {total:,} shares available",
                    inline=False,
                )

        if config.COMPANY_PUBLIC_INFO_SHOW_LEDGER:
            ledger = await self.db.get_company_ledger(company_id, limit=5)
            if ledger:
                lines = [
                    f"{'+' if amt >= 0 else ''}{fmt(amt)} — {desc} (<t:{ts}:R>)"
                    for amt, desc, _, ts in ledger
                ]
                embed.add_field(name="Recent Activity", value="\n".join(lines)[:1024], inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @company_group.command(
        name="deposit",
        description="Put your own money into the business account (never taxed, never moves the stock).",
    )
    @app_commands.describe(
        business="The business name",
        amount="Amount to deposit (e.g. 500, 10k, 1.5m) or 'all'",
        account="Where the money comes from — your cash or your bank (default: cash)",
    )
    @app_commands.choices(account=[
        app_commands.Choice(name="Cash", value="cash"),
        app_commands.Choice(name="Bank", value="bank"),
    ])
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_deposit(
        self,
        interaction: discord.Interaction,
        business: str,
        amount: str,
        account: app_commands.Choice[str] = None,
    ):
        if not config.COMPANY_DEPOSITS_ENABLED:
            await send_error(interaction, "Business deposits are disabled on this server.")
            return

        access = await self.get_access(interaction, business)
        if not access:
            return
        if not access.may(config.COMPANY_DEPOSIT_POSITIONS):
            await send_error(interaction, "Your position doesn't allow paying money into the business account.")
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = access.company
        uid, gid = interaction.user.id, interaction.guild_id
        source = (account.value if account else "cash")

        cash, bank = await self.db.get_balance(uid, gid)
        available = cash if source == "cash" else bank
        amt, error = parse_amount(
            amount,
            minimum=config.COMPANY_DEPOSIT_MIN,
            maximum=config.COMPANY_DEPOSIT_MAX,
            available=available,
            noun="deposit",
        )
        if error:
            await send_error(interaction, error)
            return

        # Take the money from the player first, in one race-safe statement; only
        # credit the business if that claim actually won.
        if not await self.db.try_spend_from(uid, gid, source, amt):
            await send_error(
                interaction,
                f"You need {fmt(amt)} in your {source} but only have {fmt(available)}.",
            )
            return

        # add_company_revenue() is deliberately NOT used here: a deposit is the
        # owner's own already-taxed money, so it must not raise revenue_total
        # (which is what the tax bill is calculated from) and must not feed
        # anything that moves the company's share price.
        await self.db.company_deposit(
            company_id, gid, amt, f"Deposit from {interaction.user.display_name}", uid
        )
        await self.db.log_transaction(uid, gid, "company_deposit", -amt, f"Deposit to {name}")

        owed = tax_owed(revenue, tax_paid)
        await interaction.response.send_message(
            f"🏦 Deposited **{fmt(amt)}** of your own {source} into **{name}**'s business account.\n"
            f"New account balance: **{fmt(balance + amt)}** — this deposit is **not taxed**, "
            f"doesn't count as revenue and doesn't affect the company's stock.\n"
            f"Outstanding tax is unchanged at {fmt(owed)}."
        )

    # ------------------------------------------------------------------ #
    @company_group.command(name="withdraw", description="Move money from the business account to your cash.")
    @app_commands.describe(business="The business name", amount="Amount to withdraw (e.g. 500, 10k) or 'all'")
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_withdraw(self, interaction: discord.Interaction, business: str, amount: str):
        access = await self.get_access(interaction, business)
        if not access:
            return
        if not access.may(config.COMPANY_WITHDRAW_POSITIONS):
            await send_error(
                interaction,
                "Your position doesn't allow taking money out of the business account.",
            )
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = access.company

        # Outstanding tax is reserved inside the business account. Without this,
        # an owner could simply empty the account and never run /tax pay, making
        # the business tax optional in practice. Owner deposits are part of the
        # balance but are not revenue, so they are fully withdrawable.
        owed = tax_owed(revenue, tax_paid)
        withdrawable = max(0, balance - owed)
        if withdrawable <= 0:
            await send_error(
                interaction,
                f"Nothing is withdrawable from **{name}** right now — the {fmt(balance)} balance is "
                f"reserved against {fmt(owed)} of outstanding tax. Settle it with `/tax pay`.",
            )
            return

        amt, error = parse_amount(amount, available=withdrawable, noun="withdrawal")
        if error:
            await send_error(interaction, error)
            return
        if amt > withdrawable:
            await send_error(
                interaction,
                f"You can withdraw at most {fmt(withdrawable)} from **{name}** — {fmt(owed)} of the "
                f"{fmt(balance)} balance is reserved for outstanding tax. Settle it with `/tax pay`.",
            )
            return

        if not await self.db.company_withdraw(company_id, amt):
            await send_error(interaction, f"The business account only has {fmt(balance)}.")
            return

        await self.db.add_cash(interaction.user.id, interaction.guild_id, amt)
        await self.db.log_transaction(
            interaction.user.id, interaction.guild_id, "company_withdraw", amt, f"From {name}"
        )
        await interaction.response.send_message(
            f"Withdrew **{fmt(amt)}** from **{name}**'s business account to your cash."
        )

    # ------------------------------------------------------------------ #
    # Staff roster
    # ------------------------------------------------------------------ #
    @company_group.command(name="staff", description="See who works at a business and what they do.")
    @app_commands.describe(business="The business name")
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_staff(self, interaction: discord.Interaction, business: str):
        access = await self.get_access(interaction, business)
        if not access:
            return

        company_id, name, owner_id, *_ = access.company
        staff = await self.db.get_company_staff(company_id)

        owner_name = "Vacant"
        if owner_id:
            member = interaction.guild.get_member(owner_id)
            owner_name = member.display_name if member else f"Unknown member ({owner_id})"

        embed = discord.Embed(title=f"👔 {name} — Staff", color=discord.Color.dark_teal())
        embed.add_field(name="Owner", value=owner_name, inline=False)

        if staff:
            # Group by seniority so the roster reads like an org chart.
            ordered = sorted(
                staff,
                key=lambda row: (position_rank(row[1]) if position_rank(row[1]) is not None else 99, row[2] or 0),
            )
            lines = []
            for user_id, position, hired_at in ordered:
                member = interaction.guild.get_member(user_id)
                display = member.display_name if member else f"Unknown member ({user_id})"
                since = f" — since <t:{hired_at}:D>" if hired_at else ""
                lines.append(f"**{position}** · {display}{since}")
            embed.add_field(
                name=f"Employees ({len(staff)}/{config.COMPANY_MAX_EMPLOYEES})",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Employees", value="Nobody hired yet — use `/company hire`.", inline=False)

        await interaction.response.send_message(embed=embed)

    async def position_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=p, value=p)
            for p in config.COMPANY_POSITIONS
            if current in p.lower()
        ][:25]

    @company_group.command(name="hire", description="Hire a player, or change an employee's position.")
    @app_commands.describe(
        business="The business name",
        user="Who to hire or re-assign",
        position="Their job title",
    )
    @app_commands.autocomplete(business=my_company_autocomplete, position=position_autocomplete)
    async def company_hire(
        self, interaction: discord.Interaction, business: str, user: discord.Member, position: str
    ):
        access = await self.get_access(interaction, business)
        if not access:
            return
        if not access.may(config.COMPANY_MANAGE_STAFF_POSITIONS):
            await send_error(interaction, "Your position doesn't allow hiring or promoting staff.")
            return

        company_id, name, owner_id, *_ = access.company

        if user.bot:
            await send_error(interaction, "Bots can't be employed.")
            return
        if user.id == owner_id:
            await send_error(
                interaction,
                f"{user.display_name} owns **{name}** — transfer ownership with `/company transfer` first.",
            )
            return

        new_rank = position_rank(position)
        if new_rank is None:
            await send_error(
                interaction,
                "Unknown position. Pick one of: " + ", ".join(f"`{p}`" for p in config.COMPANY_POSITIONS),
            )
            return
        position = config.COMPANY_POSITIONS[new_rank]  # normalise casing

        # You can only place someone in a position below your own rank.
        if not access.outranks(new_rank):
            await send_error(
                interaction,
                f"As **{access.title}** you can only assign positions below your own "
                f"({', '.join(config.COMPANY_POSITIONS[(access.rank or 0) + 1:]) or 'none'}).",
            )
            return

        existing = await self.db.get_company_staff_position(company_id, user.id)
        if existing:
            # Re-assigning someone: you must also outrank what they are now.
            if not access.outranks(position_rank(existing)):
                await send_error(
                    interaction,
                    f"{user.display_name} is **{existing}**, which you don't outrank.",
                )
                return
        else:
            staff_count = await self.db.count_company_staff(company_id)
            if staff_count >= config.COMPANY_MAX_EMPLOYEES:
                await send_error(
                    interaction,
                    f"**{name}** already has the maximum of {config.COMPANY_MAX_EMPLOYEES} employees.",
                )
                return

        await self.db.set_company_staff(
            company_id, interaction.guild_id, user.id, position, interaction.user.id
        )

        if existing and existing.lower() != position.lower():
            direction = "Promoted" if (new_rank < (position_rank(existing) or 99)) else "Moved"
            await interaction.response.send_message(
                f"👔 {direction} **{user.display_name}** from *{existing}* to **{position}** at **{name}**."
            )
        elif existing:
            await interaction.response.send_message(
                f"**{user.display_name}** is already **{position}** at **{name}**."
            )
        else:
            await interaction.response.send_message(
                f"👔 Hired **{user.display_name}** as **{position}** at **{name}**."
            )

    @company_group.command(name="fire", description="Remove an employee from a business.")
    @app_commands.describe(business="The business name", user="Who to remove")
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_fire(self, interaction: discord.Interaction, business: str, user: discord.Member):
        access = await self.get_access(interaction, business)
        if not access:
            return
        if not access.may(config.COMPANY_MANAGE_STAFF_POSITIONS):
            await send_error(interaction, "Your position doesn't allow removing staff.")
            return

        company_id, name, owner_id, *_ = access.company
        if user.id == owner_id:
            await send_error(interaction, "You can't fire the owner of the business.")
            return

        existing = await self.db.get_company_staff_position(company_id, user.id)
        if not existing:
            await send_error(interaction, f"{user.display_name} doesn't work at **{name}**.")
            return
        if not access.outranks(position_rank(existing)):
            await send_error(interaction, f"{user.display_name} is **{existing}**, which you don't outrank.")
            return

        await self.db.remove_company_staff(company_id, user.id)
        await interaction.response.send_message(
            f"👋 **{user.display_name}** ({existing}) no longer works at **{name}**."
        )

    @company_group.command(name="transfer", description="Hand your business over to another player.")
    @app_commands.describe(business="The business name", new_owner="The player taking over")
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def company_transfer(
        self, interaction: discord.Interaction, business: str, new_owner: discord.Member
    ):
        access = await self.get_access(interaction, business)
        if not access:
            return
        if not (access.is_owner or access.is_admin):
            await send_error(interaction, "Only the owner can hand the business over.")
            return
        if access.is_owner and not config.COMPANY_OWNER_CAN_TRANSFER:
            await send_error(
                interaction,
                "Owners can't transfer businesses on this server — ask economy staff to do it.",
            )
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = access.company
        if new_owner.bot:
            await send_error(interaction, "A bot can't own a business.")
            return
        if new_owner.id == owner_id:
            await send_error(interaction, f"{new_owner.display_name} already owns **{name}**.")
            return

        # The new owner can't also sit on the staff roster.
        await self.db.remove_company_staff(company_id, new_owner.id)
        await self.db.set_company_owner(company_id, new_owner.id)

        owed = tax_owed(revenue, tax_paid)
        note = (
            f"\n⚠️ **{name}** still owes {fmt(owed)} in tax — that bill goes with the business."
            if owed else ""
        )
        previous = interaction.guild.get_member(owner_id) if owner_id else None
        previous_name = previous.display_name if previous else "nobody"
        await interaction.response.send_message(
            f"📄 **{name}** has been transferred from **{previous_name}** to "
            f"**{new_owner.display_name}**, along with its {fmt(balance)} account balance.{note}"
        )

    # ------------------------------------------------------------------ #
    tax_group = app_commands.Group(name="tax", description="Business tax payments.")

    @tax_group.command(name="pay", description="Pay your business's outstanding tax.")
    @app_commands.describe(
        business="The business name",
        amount="Amount to pay (e.g. 10k) — defaults to the full outstanding tax",
    )
    @app_commands.autocomplete(business=my_company_autocomplete)
    async def tax_pay(self, interaction: discord.Interaction, business: str, amount: str = None):
        access = await self.get_access(interaction, business)
        if not access:
            return
        if not access.may(config.COMPANY_TAX_POSITIONS):
            await send_error(interaction, "Your position doesn't allow settling the business tax bill.")
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = access.company
        owed = tax_owed(revenue, tax_paid)
        if owed <= 0:
            await interaction.response.send_message(
                f"**{name}** has no outstanding tax. All-time revenue {fmt(revenue)} — "
                f"tax of {fmt(tax_paid)} already paid.",
                ephemeral=True,
            )
            return

        if amount is None:
            to_pay = owed
        else:
            parsed, error = parse_amount(amount, available=owed, maximum=owed, noun="tax payment")
            if error:
                await send_error(interaction, error)
                return
            to_pay = min(parsed, owed)

        if not await self.db.pay_company_tax(company_id, interaction.guild_id, to_pay, interaction.user.id):
            await send_error(
                interaction,
                f"The business account only has {fmt(balance)} but the payment is {fmt(to_pay)}. "
                "Customers can top it up with `/paycompany`, or you can cover it with `/company deposit`.",
            )
            return

        # Tax money goes to the server treasury.
        await self.db.add_treasury(interaction.guild_id, to_pay)
        await self.db.log_transaction(
            interaction.user.id, interaction.guild_id, "business_tax", -to_pay, f"{name} tax payment"
        )
        remaining = owed - to_pay
        await interaction.response.send_message(
            f"🧾 **{name}** paid **{fmt(to_pay)}** in tax "
            f"({config.BUSINESS_TAX_RATE * 100:.0f}% of {fmt(revenue)} all-time revenue)."
            + (f" Remaining outstanding tax: {fmt(remaining)}." if remaining else " Fully paid up!")
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Companies(bot))
