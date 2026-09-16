import time
import random
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from amounts import parse_amount as parse_shorthand, parse_count

log = logging.getLogger("beamng-eco-bot.economy")


def fmt(amount) -> str:
    """Formats a whole-dollar amount with thousands separators."""
    return f"${int(amount):,}"


def credit_rating(score: int) -> str:
    if score >= 800:
        return "Excellent"
    if score >= 740:
        return "Very Good"
    if score >= 670:
        return "Good"
    if score >= 580:
        return "Fair"
    return "Poor"


def parse_amount(raw: str) -> Optional[int]:
    """Back-compat wrapper. All shorthand parsing now lives in amounts.py so
    every command in every cog accepts exactly the same formats (10k, 1.5m,
    $2,500, ...)."""
    value, _ = parse_shorthand(raw, minimum=1)
    return value


def format_duration(seconds: int) -> str:
    hrs, rem = divmod(max(0, seconds), 3600)
    mins, secs = divmod(rem, 60)
    if hrs:
        return f"{hrs}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


async def send_error(interaction: discord.Interaction, message: str):
    """Ephemeral error reply that works whether or not the interaction was already answered."""
    if interaction.response.is_done():
        await interaction.followup.send(f"\u274c {message}", ephemeral=True)
    else:
        await interaction.response.send_message(f"\u274c {message}", ephemeral=True)


def member_has_loan_authority(member) -> bool:
    """True if this member may approve or deny loans: server Administrator,
    Manage Server or Moderate Members permission, or one of
    config.LOAN_APPROVER_ROLE_NAMES."""
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild or perms.moderate_members:
        return True
    return bool({r.name for r in member.roles}.intersection(config.LOAN_APPROVER_ROLE_NAMES))


def has_loan_authority():
    """Command gate built on member_has_loan_authority()."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if member_has_loan_authority(interaction.user):
            return True
        raise app_commands.CheckFailure(
            "You need to be a server admin or moderator to manage loan requests."
        )
    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# Loan terms: the borrower picks how long they want, and the bank prices it.
# ---------------------------------------------------------------------------
def loan_rate_for_term(days: int) -> float:
    """Interest for a loan of `days` days.

    Runs from config.LOAN_INTEREST_RATE at the shortest term up to
    config.LOAN_MAX_TERM_INTEREST_RATE at the maximum, along a curve set by
    LOAN_TERM_RATE_CURVE — so borrowing for a few extra days is cheap, but
    taking the full three weeks is deliberately expensive.
    """
    lo_days = max(1, int(config.LOAN_TERM_MIN_DAYS))
    hi_days = max(lo_days, int(config.LOAN_TERM_MAX_DAYS))
    days = max(lo_days, min(hi_days, int(days)))

    base = float(config.LOAN_INTEREST_RATE)
    top = float(config.LOAN_MAX_TERM_INTEREST_RATE)
    if hi_days == lo_days:
        return base

    progress = (days - lo_days) / (hi_days - lo_days)
    return round(base + (top - base) * (progress ** float(config.LOAN_TERM_RATE_CURVE)), 4)


def format_term(days) -> str:
    if not days:
        return "No fixed term"
    days = int(days)
    if days % 7 == 0 and days >= 7:
        weeks = days // 7
        return f"{days} days ({weeks} week{'s' if weeks > 1 else ''})"
    return f"{days} day{'s' if days != 1 else ''}"


def term_choices():
    """A few sensible terms for the /loanrequest dropdown, each labelled with
    what it actually costs, so nobody picks three weeks by accident."""
    wanted = [1, 3, 7, 10, 14, 21]
    choices = []
    for days in wanted:
        if not (config.LOAN_TERM_MIN_DAYS <= days <= config.LOAN_TERM_MAX_DAYS):
            continue
        rate = loan_rate_for_term(days)
        label = f"{format_term(days)} — {rate * 100:g}% interest"
        if days == config.LOAN_TERM_MAX_DAYS:
            label += " ⚠️"
        choices.append(app_commands.Choice(name=label[:100], value=days))
    return choices


# ---------------------------------------------------------------------------
# Loan review: the staff-facing side of a request.
#
# Every new request is posted to the channel named by LOAN_REQUEST_CHANNEL_ID
# in .env, as a full credit file — who is asking, for how much, for how long,
# what they're worth, how they've repaid before — plus a straight
# recommendation and Approve / Deny buttons.
#
# The buttons are a PERSISTENT view: their custom IDs are fixed and the request
# is found from the message it's attached to, so they still work after the bot
# restarts (which, on a free host, is often).
# ---------------------------------------------------------------------------
async def assess_request(db, guild_id: int, user_id: int, amount: int, term_days: int, rate: float):
    """Weighs up a loan request the way a bank would.

    Returns (verdict, colour, reasons, stats) where verdict is the one-line
    recommendation shown to staff and reasons explains how it got there.
    """
    cash, bank, savings, credit = await db.get_full_balance(user_id, guild_id)
    settled, settled_late = await db.get_loan_history(user_id, guild_id)
    total_due = int(amount * (1 + rate))

    credit_factor = credit / config.STARTING_CREDIT_SCORE
    max_loan = int(max(config.LOAN_MIN_AMOUNT, bank * config.LOAN_MAX_MULTIPLIER * credit_factor))
    liquid = bank + savings + cash
    coverage = (liquid / total_due) if total_due else 0
    share_of_max = (amount / max_loan) if max_loan else 1

    score, reasons = 0, []

    if credit >= 740:
        score += 2
        reasons.append(f"✅ Strong credit score ({credit}, {credit_rating(credit)})")
    elif credit >= config.STARTING_CREDIT_SCORE:
        score += 1
        reasons.append(f"✅ Decent credit score ({credit}, {credit_rating(credit)})")
    elif credit < 580:
        score -= 2
        reasons.append(f"⚠️ Weak credit score ({credit}, {credit_rating(credit)})")
    else:
        reasons.append(f"➖ Middling credit score ({credit}, {credit_rating(credit)})")

    if coverage >= 1:
        score += 2
        reasons.append(f"✅ Already holds {fmt(liquid)} — enough to cover the {fmt(total_due)} due")
    elif coverage >= 0.5:
        score += 1
        reasons.append(f"✅ Holds {fmt(liquid)}, over half of the {fmt(total_due)} due")
    elif coverage >= 0.25:
        reasons.append(f"➖ Holds {fmt(liquid)} against {fmt(total_due)} due")
    else:
        score -= 2
        reasons.append(f"⚠️ Only holds {fmt(liquid)} against {fmt(total_due)} due")

    if share_of_max <= 0.5:
        score += 2
        reasons.append(f"✅ Asking for {share_of_max * 100:.0f}% of what they're entitled to ({fmt(max_loan)})")
    elif share_of_max <= 0.8:
        score += 1
        reasons.append(f"➖ Asking for {share_of_max * 100:.0f}% of their {fmt(max_loan)} limit")
    else:
        score -= 1
        reasons.append(f"⚠️ Maxing out their limit ({share_of_max * 100:.0f}% of {fmt(max_loan)})")

    if settled_late:
        score -= 2
        reasons.append(f"⚠️ Has settled {settled_late} of {settled} previous loan(s) late")
    elif settled:
        score += 1
        reasons.append(f"✅ Repaid {settled} previous loan(s), all on time")
    else:
        reasons.append("➖ First loan — no repayment history to go on")

    if term_days and term_days >= config.LOAN_TERM_MAX_DAYS:
        score -= 1
        reasons.append(
            f"⚠️ Wants the maximum {format_term(term_days)} — the longest exposure, at {rate * 100:g}% interest"
        )

    if score >= 4:
        verdict, colour = "✅ **RECOMMENDED** — safe to approve", discord.Color.green()
    elif score >= 1:
        verdict, colour = "🟡 **BORDERLINE** — your call", discord.Color.gold()
    else:
        verdict, colour = "❌ **NOT RECOMMENDED** — likely to default", discord.Color.red()

    stats = {
        "cash": cash, "bank": bank, "savings": savings, "credit": credit,
        "max_loan": max_loan, "total_due": total_due, "settled": settled,
        "settled_late": settled_late, "score": score,
    }
    return verdict, colour, reasons, stats


async def build_review_embed(db, guild_id: int, member_label: str, user_id: int,
                             request_id: int, amount: int, term_days: int, rate: float):
    """The credit file staff see for one pending request."""
    verdict, colour, reasons, stats = await assess_request(db, guild_id, user_id, amount, term_days, rate)
    due_ts = int(time.time()) + int(term_days or 0) * 86400

    embed = discord.Embed(
        title=f"🏦 Loan Request #{request_id}",
        description=f"**{member_label}** (<@{user_id}>) is asking to borrow **{fmt(amount)}**.\n\n{verdict}",
        color=colour,
    )
    embed.add_field(name="Amount", value=fmt(amount), inline=True)
    embed.add_field(name="Term", value=format_term(term_days), inline=True)
    embed.add_field(name="Interest", value=f"{rate * 100:g}%", inline=True)
    embed.add_field(name="Total to repay", value=f"**{fmt(stats['total_due'])}**", inline=True)
    embed.add_field(name="Due if approved now", value=f"<t:{due_ts}:D> (<t:{due_ts}:R>)", inline=True)
    embed.add_field(name="Their limit", value=fmt(stats["max_loan"]), inline=True)
    embed.add_field(
        name="What they hold",
        value=(
            f"💵 Cash {fmt(stats['cash'])} · 🏦 Bank {fmt(stats['bank'])} · 📈 Savings {fmt(stats['savings'])}\n"
            f"🧾 Credit score {stats['credit']} ({credit_rating(stats['credit'])})"
        ),
        inline=False,
    )
    embed.add_field(name="Why", value="\n".join(reasons)[:1024], inline=False)
    embed.set_footer(
        text=f"User ID {user_id} · Request #{request_id} · "
             f"Approve or deny below, or use /approveloan {request_id} / /denyloan {request_id}"
    )
    return embed


async def close_review_message(bot, req, guild, decision: str, decided_by, note: str = None):
    """Rewrites the posted request once it's been decided, so the channel never
    shows a stale request with live buttons on it."""
    request_id, user_id, amount, _status, term_days, rate, guild_id = req
    channel_id, message_id = await bot.db.get_loan_request_location(request_id)
    if not (channel_id and message_id):
        return
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    approved = decision == "approved"
    embed = discord.Embed(
        title=f"{'✅' if approved else '❌'} Loan Request #{request_id} — {decision.upper()}",
        description=(
            f"<@{user_id}> · **{fmt(amount)}** over {format_term(term_days)} "
            f"at {(rate or 0) * 100:g}% interest"
        ),
        color=discord.Color.green() if approved else discord.Color.dark_grey(),
    )
    embed.add_field(name="Decided by", value=f"<@{decided_by}>", inline=True)
    embed.add_field(name="When", value=f"<t:{int(time.time())}:R>", inline=True)
    if approved:
        embed.add_field(
            name="Repayment",
            value=f"**{fmt(int(amount * (1 + (rate or 0))))}** due <t:{int(time.time()) + int(term_days or 0) * 86400}:D>",
            inline=False,
        )
    elif note:
        embed.add_field(name="Reason", value=note[:1024], inline=False)

    try:
        await message.edit(embed=embed, view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def approve_loan_request(bot, interaction: discord.Interaction, req):
    """Shared by /approveloan and the Approve button."""
    request_id, uid, amount, _status, term_days, rate, gid = req
    rate = rate if rate is not None else config.LOAN_INTEREST_RATE

    if await bot.db.get_active_loan(uid, gid):
        await bot.db.set_loan_request_status(
            request_id, "denied", interaction.user.id, "player already has an active loan"
        )
        await close_review_message(bot, req, interaction.guild, "denied", interaction.user.id,
                                   "The borrower already has an active loan.")
        await send_error(interaction, "That player already has an active loan. Request auto-denied.")
        return

    loan_id = await bot.db.create_loan(uid, gid, amount, rate, term_days)
    await bot.db.add_cash(uid, gid, amount)
    await bot.db.log_transaction(
        uid, gid, "loan_issued", amount, f"Loan #{loan_id}, approved by {interaction.user.display_name}"
    )
    await bot.db.set_loan_request_status(request_id, "approved", interaction.user.id)

    total_due = int(amount * (1 + rate))
    due_ts = int(time.time()) + int(term_days or 0) * 86400

    embed = discord.Embed(title="💳 Loan Approved", color=discord.Color.green())
    embed.add_field(name="Borrower", value=f"<@{uid}>", inline=True)
    embed.add_field(name="Issued (cash)", value=fmt(amount), inline=True)
    embed.add_field(name="Term", value=format_term(term_days), inline=True)
    embed.add_field(name=f"Total Due ({rate * 100:g}% interest)", value=f"**{fmt(total_due)}**", inline=True)
    if term_days:
        embed.add_field(name="Repay by", value=f"<t:{due_ts}:D> (<t:{due_ts}:R>)", inline=True)
    embed.set_footer(text=f"Approved by {interaction.user.display_name} · Request #{request_id}")

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
    else:
        await interaction.response.send_message(embed=embed)
    await close_review_message(bot, req, interaction.guild, "approved", interaction.user.id)


async def deny_loan_request(bot, interaction: discord.Interaction, req, reason: str):
    """Shared by /denyloan and the Deny button."""
    request_id, uid, amount, _status, term_days, rate, gid = req
    await bot.db.set_loan_request_status(request_id, "denied", interaction.user.id, reason)

    message = f"❌ Denied loan request #{request_id} (<@{uid}>, {fmt(amount)}). Reason: {reason}"
    if interaction.response.is_done():
        await interaction.followup.send(message)
    else:
        await interaction.response.send_message(message)
    await close_review_message(bot, req, interaction.guild, "denied", interaction.user.id, reason)


class DenyReasonModal(discord.ui.Modal, title="Deny loan request"):
    reason = discord.ui.TextInput(
        label="Reason (the borrower will see this)",
        placeholder="e.g. Too soon after the last loan — try again next week.",
        required=False,
        max_length=300,
    )

    def __init__(self, bot, req):
        super().__init__()
        self.bot = bot
        self.req = req

    async def on_submit(self, interaction: discord.Interaction):
        await deny_loan_request(
            self.bot, interaction, self.req, str(self.reason.value).strip() or "No reason given"
        )


class LoanReviewView(discord.ui.View):
    """Approve / Deny buttons attached to a posted loan request.

    Persistent: fixed custom IDs and no timeout, with the request looked up from
    the message the buttons are on, so a restart doesn't leave dead buttons in
    the loan channel.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _resolve(self, interaction: discord.Interaction):
        """The pending request behind this message, or None (having already
        explained to the clicker why not)."""
        if not member_has_loan_authority(interaction.user):
            await interaction.response.send_message(
                "You need to be a server admin or moderator to review loan requests.", ephemeral=True
            )
            return None

        req = await self.bot.db.get_loan_request_by_message(interaction.message.id)
        if not req:
            await interaction.response.send_message(
                "I can't find the request behind this message any more. Use `/loanrequests` instead.",
                ephemeral=True,
            )
            return None
        if req[3] != "pending":
            await interaction.response.send_message(
                f"Request #{req[0]} has already been {req[3]}.", ephemeral=True
            )
            return None
        return req

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success,
                       emoji="\u2705", custom_id="loan_review:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        req = await self._resolve(interaction)
        if req:
            await approve_loan_request(self.bot, interaction, req)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger,
                       emoji="\u274c", custom_id="loan_review:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        req = await self._resolve(interaction)
        if req:
            await interaction.response.send_modal(DenyReasonModal(self.bot, req))


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.savings_interest_tick.start()
        self.overdue_loan_tick.start()

    async def cog_load(self):
        # Re-arm the Approve/Deny buttons on every loan request already sitting
        # in the loan channel from before this restart.
        self.bot.add_view(LoanReviewView(self.bot))

    def cog_unload(self):
        self.savings_interest_tick.cancel()
        self.overdue_loan_tick.cancel()

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    # Background task: pays savings interest once every SAVINGS_TICK_HOURS.
    # Runs hourly and compares against the persisted last-payout timestamp,
    # so bot restarts never double-pay or skip a cycle.
    # ------------------------------------------------------------------ #
    @tasks.loop(hours=1)
    async def savings_interest_tick(self):
        now = int(time.time())
        last_tick = int(await self.db.get_meta("last_savings_tick", 0) or 0)
        if last_tick == 0:
            await self.db.set_meta("last_savings_tick", now)  # first boot: start the clock
            return
        if now - last_tick < config.SAVINGS_TICK_HOURS * 3600:
            return

        for guild_id in await self.db.get_all_guild_ids():
            await self.db.pay_savings_interest(guild_id, config.SAVINGS_INTEREST_RATE)
        await self.db.set_meta("last_savings_tick", now)

    @savings_interest_tick.before_loop
    async def before_savings_interest_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Background task: reminds staff about loans that have run past the
    # deadline the borrower agreed to. Nothing is seized automatically — this
    # just puts it in front of the people who can act on it in character.
    # ------------------------------------------------------------------ #
    @tasks.loop(hours=1)
    async def overdue_loan_tick(self):
        if not config.LOAN_OVERDUE_ALERTS:
            return
        channel_id = config.loan_request_channel_id()
        if not channel_id:
            return

        now = int(time.time())
        last = int(await self.db.get_meta("last_overdue_alert", 0) or 0)
        interval = max(1, int(config.LOAN_OVERDUE_ALERT_HOURS)) * 3600
        if last and now - last < interval:
            return
        await self.db.set_meta("last_overdue_alert", now)
        if not last:
            return  # first boot: start the clock rather than alerting instantly

        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        for guild_id in await self.db.get_all_guild_ids():
            overdue = await self.db.get_overdue_loans(guild_id, now)
            if not overdue:
                continue
            lines = [
                f"<@{user_id}> — **{fmt(remaining)}** outstanding, due <t:{due_at}:R>"
                for _lid, user_id, _principal, remaining, due_at in overdue[:20]
            ]
            embed = discord.Embed(
                title="\u23f0 Overdue Loans",
                description="\n".join(lines),
                color=discord.Color.dark_red(),
            )
            embed.set_footer(
                text=f"{len(overdue)} loan(s) past their deadline. Nothing has been taken automatically."
            )
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @overdue_loan_tick.before_loop
    async def before_overdue_loan_tick(self):
        await self.bot.wait_until_ready()

    async def _next_interest_timestamp(self) -> Optional[int]:
        last_tick = int(await self.db.get_meta("last_savings_tick", 0) or 0)
        if not last_tick:
            return None
        return last_tick + config.SAVINGS_TICK_HOURS * 3600

    # ------------------------------------------------------------------ #
    # /balance, /deposit and /withdraw each have a short alias (/bal, /dep,
    # /with) for people who type them twenty times a session. Both names run
    # the same code below, so they can never drift apart.
    # ------------------------------------------------------------------ #
    @app_commands.command(name="balance", description="View a full financial profile: cash, bank, savings, debt and credit.")
    @app_commands.describe(user="Whose profile to view (defaults to you)")
    async def balance(self, interaction: discord.Interaction, user: discord.Member = None):
        await self.show_balance(interaction, user)

    @app_commands.command(name="bal", description="Shortcut for /balance — cash, bank, savings, debt and credit.")
    @app_commands.describe(user="Whose profile to view (defaults to you)")
    async def bal(self, interaction: discord.Interaction, user: discord.Member = None):
        await self.show_balance(interaction, user)

    async def show_balance(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        uid, gid = target.id, interaction.guild_id

        cash, bank, savings, credit = await self.db.get_full_balance(uid, gid)
        loan = await self.db.get_active_loan(uid, gid)
        debt = loan[2] if loan else 0
        loan_due_at = loan[4] if loan else None
        garage_value = await self.db.get_vehicle_value(uid, gid)
        net_worth = cash + bank + savings + garage_value - debt

        embed = discord.Embed(
            title=f"{config.ECONOMY_BRAND_NAME} Financial Profile",
            description=f"{target.mention}",
            color=discord.Color.green() if net_worth >= 0 else discord.Color.red(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="\U0001f4b5 Cash", value=fmt(cash))
        embed.add_field(name=f"\U0001f3e6 {config.ECONOMY_BRAND_NAME} Bank", value=fmt(bank))
        embed.add_field(name="\U0001f4c8 Savings", value=fmt(savings))
        embed.add_field(name="\U0001f697 Garage Value", value=fmt(garage_value))
        if debt and loan_due_at:
            overdue = loan_due_at < int(time.time())
            debt_value = f"{fmt(debt)}\n{'⚠️ overdue' if overdue else 'due'} <t:{loan_due_at}:R>"
        else:
            debt_value = fmt(debt) if debt else "None"
        embed.add_field(name="\U0001f4b3 Loan Debt", value=debt_value)
        embed.add_field(name="\U0001f9fe Credit Score", value=f"{credit} ({credit_rating(credit)})")
        embed.add_field(name="Net Worth", value=f"**{fmt(net_worth)}**", inline=False)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="daily", description="Claim your daily reward (builds a streak bonus).")
    async def daily(self, interaction: discord.Interaction):
        if not config.DAILY_ENABLED:
            await send_error(interaction, "The daily reward is currently disabled on this server.")
            return

        uid, gid = interaction.user.id, interaction.guild_id
        now = int(time.time())
        cooldown_secs = int(config.DAILY_COOLDOWN_HOURS * 3600)
        grace_secs = int(config.DAILY_STREAK_GRACE_HOURS * 3600)

        # Claim the cooldown BEFORE paying out, in one statement, so several
        # simultaneous /daily commands can't all read the same stale timestamp
        # and all get paid — the same pattern used by /work.
        last_daily, _ = await self.db.get_daily_state(uid, gid)
        streak = await self.db.try_consume_daily(uid, gid, now, cooldown_secs, grace_secs)
        if streak is None:
            remaining = max(1, cooldown_secs - (now - (last_daily or 0)))
            await send_error(
                interaction,
                f"You've already claimed today's reward. Come back in {format_duration(remaining)}.",
            )
            return

        base = random.randint(config.DAILY_REWARD_MIN, config.DAILY_REWARD_MAX)
        bonus_days = min(max(streak - 1, 0), max(config.DAILY_STREAK_MAX_DAYS - 1, 0))
        bonus = bonus_days * config.DAILY_STREAK_BONUS
        total = base + bonus

        if config.DAILY_PAY_TO_BANK:
            await self.db.add_bank(uid, gid, total)
            destination = f"{config.ECONOMY_BRAND_NAME} Bank account"
        else:
            await self.db.add_cash(uid, gid, total)
            destination = "wallet"
        await self.db.log_transaction(uid, gid, "daily", total, f"Daily reward (streak {streak})")

        embed = discord.Embed(
            title="\U0001f4b0 Daily Reward",
            description=f"**{fmt(total)}** added to your {destination}.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Base", value=fmt(base))
        embed.add_field(name="Streak Bonus", value=fmt(bonus) if bonus else "None")
        embed.add_field(name="Streak", value=f"{streak} day(s)")
        embed.set_footer(
            text=f"Come back within {config.DAILY_STREAK_GRACE_HOURS}h of this claim to keep your streak alive."
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="work", description="Do a quick odd job for some cash (short cooldown).")
    async def work(self, interaction: discord.Interaction):
        uid, gid = interaction.user.id, interaction.guild_id
        now = int(time.time())
        cooldown_secs = config.WORK_COOLDOWN_MINUTES * 60

        # Claim the cooldown BEFORE paying out. The check and the write are a
        # single statement, so simultaneous /work commands can't all read the
        # same stale timestamp and all get paid.
        if not await self.db.try_consume_work_cooldown(uid, gid, now, cooldown_secs):
            _, last_work = await self.db.get_cooldowns(uid, gid)
            remaining = max(1, cooldown_secs - (now - (last_work or 0)))
            await send_error(interaction, f"You're still tired from the last job. Try again in {format_duration(remaining)}.")
            return

        earnings = random.randint(config.WORK_MIN, config.WORK_MAX)
        flavor = random.choice([
            "You helped push a stalled car off the highway.",
            "You detailed a customer's car at the gas station.",
            "You flagged traffic around a fender bender.",
            "You ran spare parts across town for a garage.",
            "You washed cars at the local car wash.",
            "You towed a wreck out of a ditch for a stranded driver.",
            "You changed a set of tires at the roadside.",
        ])
        await self.db.add_cash(uid, gid, earnings)
        await self.db.log_transaction(uid, gid, "work", earnings, "Odd job")
        await interaction.response.send_message(f"{flavor}\nYou earned **{fmt(earnings)}**.")

    # ------------------------------------------------------------------ #
    @app_commands.command(name="deposit", description="Move cash into your bank account.")
    @app_commands.describe(amount="Amount to deposit (e.g. 500, 1.5k) or 'all'")
    async def deposit(self, interaction: discord.Interaction, amount: str):
        await self.do_deposit(interaction, amount)

    @app_commands.command(name="dep", description="Shortcut for /deposit — move cash into your bank account.")
    @app_commands.describe(amount="Amount to deposit (e.g. 500, 1.5k) or 'all'")
    async def dep(self, interaction: discord.Interaction, amount: str):
        await self.do_deposit(interaction, amount)

    async def do_deposit(self, interaction: discord.Interaction, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        cash, _ = await self.db.get_balance(uid, gid)

        amt, error = parse_shorthand(amount, available=cash, noun="deposit")
        if error:
            await send_error(interaction, error)
            return
        if not await self.db.move_funds(uid, gid, "cash", "bank", amt):
            await send_error(interaction, f"You only have {fmt(cash)} in cash.")
            return

        await self.db.log_transaction(uid, gid, "deposit", amt, "Cash -> Bank")
        await interaction.response.send_message(f"Deposited **{fmt(amt)}** into your bank.")

    # ------------------------------------------------------------------ #
    @app_commands.command(name="withdraw", description="Move money from your bank into cash.")
    @app_commands.describe(amount="Amount to withdraw (e.g. 500, 1.5k) or 'all'")
    async def withdraw(self, interaction: discord.Interaction, amount: str):
        await self.do_withdraw(interaction, amount)

    @app_commands.command(name="with", description="Shortcut for /withdraw — move bank money into cash.")
    @app_commands.describe(amount="Amount to withdraw (e.g. 500, 1.5k) or 'all'")
    async def with_shortcut(self, interaction: discord.Interaction, amount: str):
        await self.do_withdraw(interaction, amount)

    async def do_withdraw(self, interaction: discord.Interaction, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        _, bank = await self.db.get_balance(uid, gid)

        amt, error = parse_shorthand(amount, available=bank, noun="withdrawal")
        if error:
            await send_error(interaction, error)
            return
        if not await self.db.move_funds(uid, gid, "bank", "cash", amt):
            await send_error(interaction, f"You only have {fmt(bank)} in the bank.")
            return

        await self.db.log_transaction(uid, gid, "withdraw", amt, "Bank -> Cash")
        await interaction.response.send_message(f"Withdrew **{fmt(amt)}** to your cash on hand.")

    # ------------------------------------------------------------------ #
    savings_group = app_commands.Group(
        name="savings",
        description=f"Savings account that earns {config.SAVINGS_INTEREST_RATE * 100:g}% interest every {config.SAVINGS_TICK_HOURS}h.",
    )

    @savings_group.command(name="deposit", description="Move cash into your interest-earning savings account.")
    @app_commands.describe(amount="Amount to deposit (e.g. 500, 1.5k) or 'all'")
    async def savings_deposit(self, interaction: discord.Interaction, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        cash, _ = await self.db.get_balance(uid, gid)

        amt, error = parse_shorthand(amount, available=cash, noun="deposit")
        if error:
            await send_error(interaction, error)
            return
        if not await self.db.move_funds(uid, gid, "cash", "savings", amt):
            await send_error(interaction, f"You only have {fmt(cash)} in cash.")
            return

        await self.db.log_transaction(uid, gid, "savings_deposit", amt, "Cash -> Savings")
        savings = await self.db.get_savings(uid, gid)
        await interaction.response.send_message(
            f"Moved **{fmt(amt)}** into savings. Savings balance: **{fmt(savings)}**."
        )

    @savings_group.command(name="withdraw", description="Move money from savings back to cash.")
    @app_commands.describe(amount="Amount to withdraw (e.g. 500, 1.5k) or 'all'")
    async def savings_withdraw(self, interaction: discord.Interaction, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        savings = await self.db.get_savings(uid, gid)

        amt, error = parse_shorthand(amount, available=savings, noun="withdrawal")
        if error:
            await send_error(interaction, error)
            return
        if not await self.db.move_funds(uid, gid, "savings", "cash", amt):
            await send_error(interaction, f"You only have {fmt(savings)} in savings.")
            return

        await self.db.log_transaction(uid, gid, "savings_withdraw", amt, "Savings -> Cash")
        await interaction.response.send_message(f"Withdrew **{fmt(amt)}** from savings to cash.")

    @savings_group.command(name="info", description="See your savings balance, interest rate and next payout.")
    async def savings_info(self, interaction: discord.Interaction):
        uid, gid = interaction.user.id, interaction.guild_id
        savings = await self.db.get_savings(uid, gid)
        projected = int(savings * config.SAVINGS_INTEREST_RATE)
        next_ts = await self._next_interest_timestamp()

        embed = discord.Embed(title="\U0001f4c8 Savings Account", color=discord.Color.teal())
        embed.add_field(name="Balance", value=fmt(savings))
        embed.add_field(name="Interest Rate", value=f"{config.SAVINGS_INTEREST_RATE * 100:g}% / {config.SAVINGS_TICK_HOURS}h")
        embed.add_field(name="Next Payout", value=f"~{fmt(projected)} <t:{next_ts}:R>" if next_ts else "Pending first cycle", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="pay", description="Pay another player from your cash on hand.")
    @app_commands.describe(user="Who to pay", amount="Amount of cash to send (e.g. 500, 10k, 1.5m) or 'all'")
    async def pay(self, interaction: discord.Interaction, user: discord.Member, amount: str):
        if user.id == interaction.user.id:
            await send_error(interaction, "You can't pay yourself.")
            return
        if user.bot:
            await send_error(interaction, "You can't pay a bot.")
            return

        uid, gid = interaction.user.id, interaction.guild_id
        cash, _ = await self.db.get_balance(uid, gid)
        amt, error = parse_shorthand(amount, available=cash, noun="payment")
        if error:
            await send_error(interaction, error)
            return

        if not await self.db.transfer_cash(uid, user.id, gid, amt):
            await send_error(interaction, f"You only have {fmt(cash)} in cash.")
            return

        await self.db.log_transaction(uid, gid, "pay_sent", -amt, f"Paid {user.display_name}")
        await self.db.log_transaction(user.id, gid, "pay_received", amt, f"From {interaction.user.display_name}")
        await interaction.response.send_message(f"\U0001f4b8 You paid **{fmt(amt)}** to {user.mention}.")

    # ------------------------------------------------------------------ #
    @app_commands.command(name="leaderboard", description="See the richest players on the server (cash + bank + savings).")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.db.leaderboard(interaction.guild_id, limit=10)
        if not rows:
            await interaction.response.send_message("No economy data yet.")
            return

        medals = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
        lines = []
        for i, (user_id, cash, bank, savings) in enumerate(rows, start=1):
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            prefix = medals.get(i, f"**{i}.**")
            lines.append(f"{prefix} {name} \u00b7 {fmt(cash + bank + savings)}")

        embed = discord.Embed(title="\U0001f4b0 Richest Players", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="transactions",
        description="View your recent transaction history (staff can look up any player).",
    )
    @app_commands.describe(
        user="Whose history to view — staff only, defaults to you",
        limit="How many entries to show (5-50)",
    )
    async def transactions(
        self,
        interaction: discord.Interaction,
        user: discord.Member = None,
        limit: app_commands.Range[int, 5, 50] = 10,
    ):
        # Imported here rather than at the top: cogs.admin imports this module,
        # so a module-level import would be circular.
        from cogs.admin import is_eco_staff

        target = user or interaction.user
        looking_at_someone_else = target.id != interaction.user.id

        if looking_at_someone_else:
            if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
                await send_error(interaction, "This command can only be used in a server.")
                return
            if not is_eco_staff(interaction.user, interaction.guild):
                await send_error(
                    interaction,
                    "Only economy staff can look up another player's transactions. "
                    "Leave the `user` option blank to see your own.",
                )
                return

        rows = await self.db.get_transactions(target.id, interaction.guild_id, limit=limit)
        if not rows:
            who = "That player has no transactions yet." if looking_at_someone_else else "No transactions yet."
            await interaction.response.send_message(who, ephemeral=True)
            return

        lines = []
        for ttype, amount, desc, ts in rows:
            sign = "+" if amount >= 0 else "\u2212"
            label = ttype.replace("_", " ")
            detail = f" \u00b7 {desc}" if desc else ""
            lines.append(f"`{sign}{fmt(abs(amount))}` {label}{detail} <t:{ts}:R>")

        # Discord caps an embed description at 4096 characters; drop the oldest
        # entries rather than letting a 50-row lookup fail outright.
        description = "\n".join(lines)
        while len(description) > 3900 and len(lines) > 1:
            lines.pop()
            description = "\n".join(lines) + "\n*(older entries trimmed to fit)*"

        title = f"Recent Transactions \u2014 {target.display_name}" if looking_at_someone_else \
            else "Recent Transactions"
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())

        if looking_at_someone_else:
            cash, bank = await self.db.get_balance(target.id, interaction.guild_id)
            money_in = sum(a for _, a, _, _ in rows if a > 0)
            money_out = sum(-a for _, a, _, _ in rows if a < 0)
            embed.add_field(
                name="Across these entries",
                value=f"In: **{fmt(money_in)}** \u00b7 Out: **{fmt(money_out)}** "
                      f"\u00b7 Net: **{fmt(money_in - money_out)}**",
                inline=False,
            )
            embed.add_field(
                name="Balance now",
                value=f"Cash **{fmt(cash)}** \u00b7 Bank **{fmt(bank)}**",
                inline=False,
            )
            embed.set_footer(text=f"Staff lookup by {interaction.user.display_name}")
            if target.display_avatar:
                embed.set_thumbnail(url=target.display_avatar.url)

        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Looking at your own history is private and not worth logging. Staff
        # reading someone else's is an administrative action, so it goes in the
        # audit channel like every other one.
        if looking_at_someone_else:
            import audit
            await audit.send_log(self.bot, interaction.guild_id, discord.Embed(
                title="Transaction history viewed",
                description=(
                    f"{interaction.user.mention} looked up the last {len(rows)} transactions "
                    f"for {target.mention}."
                ),
                color=discord.Color.greyple(),
            ))

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="loanrequest",
        description="Request a bank loan for staff to review (amount is capped by your bank balance and credit score).",
    )
    @app_commands.describe(
        amount="Amount to borrow (e.g. 5000, 10k, 1.5m)",
        days=f"How long you want to repay it in — longer terms cost much more interest "
             f"(max {config.LOAN_TERM_MAX_DAYS} days)",
    )
    @app_commands.choices(days=term_choices())
    async def loanrequest(
        self,
        interaction: discord.Interaction,
        amount: str,
        days: app_commands.Choice[int] = None,
    ):
        uid, gid = interaction.user.id, interaction.guild_id
        amount, error = parse_shorthand(amount, minimum=1, noun="loan amount")
        if error:
            await send_error(interaction, error)
            return

        term_days = days.value if days else config.LOAN_TERM_DEFAULT_DAYS
        # Belt and braces: the dropdown can't offer a longer term, but the cap
        # is enforced here too so it holds however the command is called.
        term_days = max(config.LOAN_TERM_MIN_DAYS, min(config.LOAN_TERM_MAX_DAYS, int(term_days)))
        rate = loan_rate_for_term(term_days)
        existing = await self.db.get_active_loan(uid, gid)
        if existing:
            await send_error(
                interaction,
                f"You already have an active loan with **{fmt(existing[2])}** remaining. Pay it off first with `/loanpay`.",
            )
            return

        pending = await self.db.get_pending_loan_request_for_user(uid, gid)
        if pending:
            await send_error(
                interaction,
                f"You already have a pending loan request (`#{pending[0]}`) for **{fmt(pending[1])}**. "
                "Wait for a staff member to review it.",
            )
            return

        if amount < config.LOAN_MIN_AMOUNT:
            await send_error(interaction, f"Minimum loan amount is {fmt(config.LOAN_MIN_AMOUNT)}.")
            return

        _, bank, _, credit = await self.db.get_full_balance(uid, gid)
        if credit < config.LOAN_MIN_CREDIT_SCORE:
            await send_error(
                interaction,
                f"Your credit score ({credit}) is below the bank's minimum of {config.LOAN_MIN_CREDIT_SCORE}. "
                "Pay fines on time and clear loans to rebuild it.",
            )
            return

        credit_factor = credit / config.STARTING_CREDIT_SCORE
        max_loan = int(max(config.LOAN_MIN_AMOUNT, bank * config.LOAN_MAX_MULTIPLIER * credit_factor))
        if amount > max_loan:
            await send_error(interaction, f"You can request at most {fmt(max_loan)} based on your bank balance and credit score.")
            return

        request_id = await self.db.create_loan_request(uid, gid, amount, term_days, rate)
        total_due = int(amount * (1 + rate))
        due_ts = int(time.time()) + term_days * 86400

        embed = discord.Embed(title="\U0001f4dd Loan Request Submitted", color=discord.Color.orange())
        embed.add_field(name="Requested", value=fmt(amount))
        embed.add_field(name="Repayment term", value=format_term(term_days))
        embed.add_field(name=f"Total due ({rate * 100:g}% interest)", value=f"**{fmt(total_due)}**")
        embed.add_field(
            name="Deadline if approved now",
            value=f"<t:{due_ts}:D> (<t:{due_ts}:R>)",
            inline=False,
        )
        if term_days >= config.LOAN_TERM_MAX_DAYS:
            embed.add_field(
                name="\u26a0\ufe0f You picked the longest term",
                value=(
                    f"Three weeks is the most the bank will lend for, and it charges "
                    f"{rate * 100:g}% for it — {fmt(total_due - amount)} of interest on top of what you "
                    "borrowed. A shorter term is far cheaper if you can manage it."
                ),
                inline=False,
            )
        embed.set_footer(
            text=f"Request #{request_id} — a server admin or moderator must approve it before funds are issued."
        )
        await interaction.response.send_message(embed=embed)

        await self.post_loan_request(interaction, request_id, amount, term_days, rate)

    async def post_loan_request(self, interaction: discord.Interaction, request_id: int,
                                amount: int, term_days: int, rate: float):
        """Sends the request to the staff loan channel (LOAN_REQUEST_CHANNEL_ID
        in .env) with the full credit file and the review buttons.

        Never raises: if the channel is missing or the bot can't post there, the
        request still stands and staff can find it with /loanrequests.
        """
        channel_id = config.loan_request_channel_id()
        if not channel_id:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("LOAN_REQUEST_CHANNEL_ID %s is not a channel I can see.", channel_id)
            return

        try:
            embed = await build_review_embed(
                self.db, interaction.guild_id, interaction.user.display_name,
                interaction.user.id, request_id, amount, term_days, rate,
            )
            message = await channel.send(embed=embed, view=LoanReviewView(self.bot))
            await self.db.set_loan_request_message(request_id, channel.id, message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not post loan request #%s to the loan channel: %s", request_id, exc)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="loanrequests", description="[Staff] View pending loan requests awaiting approval.")
    @has_loan_authority()
    async def loanrequests(self, interaction: discord.Interaction):
        rows = await self.db.get_pending_loan_requests(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No pending loan requests.", ephemeral=True)
            return

        lines = []
        for rid, uid, amount, ts, term_days, rate in rows:
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            terms = (
                f" \u00b7 {format_term(term_days)} at {rate * 100:g}%"
                if term_days and rate is not None else ""
            )
            lines.append(f"`#{rid}` {name} \u2014 {fmt(amount)}{terms} \u00b7 requested <t:{ts}:R>")

        embed = discord.Embed(
            title="\U0001f4cb Pending Loan Requests",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Use /approveloan <id> or /denyloan <id> to review.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="approveloan", description="[Staff] Approve a pending loan request and issue the funds.")
    @app_commands.describe(request_id="The loan request ID (see /loanrequests)")
    @has_loan_authority()
    async def approveloan(self, interaction: discord.Interaction, request_id: int):
        req = await self.db.get_loan_request(request_id, interaction.guild_id)
        if not req or req[3] != "pending":
            await send_error(interaction, "No pending loan request with that ID.")
            return
        await approve_loan_request(self.bot, interaction, req)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="denyloan", description="[Staff] Deny a pending loan request.")
    @app_commands.describe(request_id="The loan request ID (see /loanrequests)", reason="Optional reason shown to the player")
    @has_loan_authority()
    async def denyloan(self, interaction: discord.Interaction, request_id: int, reason: str = "No reason given"):
        req = await self.db.get_loan_request(request_id, interaction.guild_id)
        if not req or req[3] != "pending":
            await send_error(interaction, "No pending loan request with that ID.")
            return
        await deny_loan_request(self.bot, interaction, req, reason)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You don't have permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        else:
            raise error

    @app_commands.command(name="loanstatus", description="Check your current loan balance.")
    async def loanstatus(self, interaction: discord.Interaction):
        loan = await self.db.get_active_loan(interaction.user.id, interaction.guild_id)
        if not loan:
            await interaction.response.send_message("You have no active loan.", ephemeral=True)
            return

        _, principal, remaining, rate, due_at, term_days = loan
        overdue = bool(due_at and due_at < int(time.time()))

        embed = discord.Embed(
            title="\U0001f4b3 Loan Status",
            color=discord.Color.red() if overdue else discord.Color.orange(),
        )
        embed.add_field(name="Principal", value=fmt(principal))
        embed.add_field(name="Remaining", value=f"**{fmt(remaining)}**")
        embed.add_field(name="Interest Rate", value=f"{rate * 100:g}%")
        if due_at:
            embed.add_field(name="Agreed term", value=format_term(term_days))
            embed.add_field(
                name="\u26a0\ufe0f Overdue since" if overdue else "Repay by",
                value=f"<t:{due_at}:D> (<t:{due_at}:R>)",
                inline=False,
            )
            if overdue:
                embed.set_footer(
                    text=f"Settling late costs you {config.LOAN_LATE_PAYOFF_PENALTY} credit score points. "
                         "Pay it down with /loanpay."
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="loanpay", description="Pay down your active loan using cash.")
    @app_commands.describe(amount="Amount of cash to put toward your loan (or 'all')")
    async def loanpay(self, interaction: discord.Interaction, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        loan = await self.db.get_active_loan(uid, gid)
        if not loan:
            await send_error(interaction, "You have no active loan.")
            return

        loan_id, principal, remaining, rate, due_at, term_days = loan
        cash, _ = await self.db.get_balance(uid, gid)
        amt, error = parse_shorthand(amount, available=min(cash, remaining), noun="payment")
        if error:
            await send_error(interaction, error)
            return

        payment = min(amt, remaining)
        if not await self.db.try_spend_cash(uid, gid, payment):
            await send_error(interaction, f"You only have {fmt(cash)} in cash.")
            return

        new_remaining = await self.db.pay_loan(loan_id, payment)
        await self.db.log_transaction(uid, gid, "loan_payment", -payment, f"Loan #{loan_id}")

        if new_remaining == 0:
            # Settling on time builds credit; settling late costs it. The
            # deadline the borrower chose is what makes the two different.
            late = bool(due_at and int(time.time()) > due_at)
            delta = -config.LOAN_LATE_PAYOFF_PENALTY if late else config.CREDIT_SCORE_LOAN_PAYOFF_BONUS
            new_score = await self.db.adjust_credit_score(
                uid, gid, delta, config.MIN_CREDIT_SCORE, config.MAX_CREDIT_SCORE
            )
            verdict = (
                f"\u26a0\ufe0f It was settled **late** — credit score {delta} "
                f"(now {new_score}, {credit_rating(new_score)})."
                if late else
                f"Credit score +{config.CREDIT_SCORE_LOAN_PAYOFF_BONUS} "
                f"(now {new_score}, {credit_rating(new_score)})."
            )
            await interaction.response.send_message(
                f"Paid **{fmt(payment)}**. Your loan is fully paid off! \U0001f389\n{verdict}"
            )
        else:
            deadline = f"\nDue <t:{due_at}:R>." if due_at else ""
            await interaction.response.send_message(
                f"Paid **{fmt(payment)}** toward your loan. Remaining: **{fmt(new_remaining)}**.{deadline}"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
