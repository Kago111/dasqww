"""
Audit logging: mirrors everything players and staff do with the bot into a
single Discord channel, set per server with /eco-admin log-channel.

send_log() is deliberately failure-tolerant — a missing channel or revoked
permission must never break the command that triggered the log.

The second half of this module is the ECONOMY RECONCILIATION AUDIT
(run_reconciliation), run by /eco-admin audit and on a timer from cogs/admin.py:

  * Money supply drift — does the money that exists match what the transaction
    log says was minted and burned since the last check? If not, something
    duplicated or destroyed money without leaving a record.
  * Suspicious trading patterns — players hammering the per-order cap, and
    businesses paid by throwaway accounts right before an insider sells.
"""

import json
import logging
import time

import discord

import config

log = logging.getLogger("beamng-eco-bot.audit")

# Commands that reveal nothing and would only spam the audit channel.
IGNORED_COMMANDS = {
    "balance", "market", "topmovers", "stockinfo", "portfolio", "leaderboard",
    "garage", "joblist", "jobinfo", "fines", "loanstatus", "loanrequests",
    "transactions", "wantedstatus", "savings info", "company balance", "bal",
    "company staff", "eco-admin treasury", "eco-admin company info",
    "eco-admin company ledger", "company list", "company info",
    "eco-admin channels show",
}

# Commands that move money or change server state get a louder colour.
IMPORTANT_COMMANDS = {
    "eco-admin give", "eco-admin take", "eco-admin reset", "eco-admin company create",
    "eco-admin company addrevenue", "eco-admin company delete", "eco-admin business create",
    "eco-admin business link", "eco-admin business unlink", "eco-admin business adjust",
    "approveloan", "denyloan", "loanrequest",
    "eco-admin business delist", "eco-admin business setowner", "eco-admin givevehicle",
    "eco-admin removevehicle", "eco-admin credit-adjust", "eco-admin log-channel",
    "eco-admin market-channel", "eco-admin setjob", "tax pay", "migrate-unbelievaboat",
    "eco-admin company setowner", "eco-admin company setstaff", "company transfer",
    "company deposit", "company withdraw", "company hire", "company fire",
    "eco-admin reset-economy", "eco-admin channels add", "eco-admin channels remove",
    "eco-admin channels clear", "eco-admin audit", "stock history",
    "eco-admin db prepare-update", "eco-admin db verify-update",
}


async def send_log(bot, guild_id: int, embed: discord.Embed):
    """Posts an embed to the guild's configured log channel, if there is one."""
    if not guild_id:
        return
    try:
        channel_id = await bot.db.get_log_channel(guild_id)
        if not channel_id:
            return
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Could not write to the audit log channel for guild %s: %s", guild_id, exc)
    except Exception:
        log.exception("Unexpected error writing to the audit log channel for guild %s", guild_id)


def format_options(interaction: discord.Interaction) -> str:
    """Renders the options the user supplied, e.g. `business: Acme | shares: 50`."""
    parts = []
    for name, value in interaction.namespace:
        if isinstance(value, (discord.Member, discord.User)):
            shown = f"{value} ({value.id})"
        elif isinstance(value, (discord.abc.GuildChannel, discord.Thread)):
            shown = f"#{value.name} ({value.id})"
        else:
            shown = str(value)
        if len(shown) > 200:
            shown = shown[:197] + "..."
        parts.append(f"`{name}`: {shown}")
    return " | ".join(parts) if parts else "*no options*"


async def log_command(bot, interaction: discord.Interaction, command):
    """Records a successfully completed application command."""
    name = command.qualified_name
    if name in IGNORED_COMMANDS:
        return

    color = discord.Color.orange() if name in IMPORTANT_COMMANDS else discord.Color.blurple()
    embed = discord.Embed(title=f"/{name}", color=color, timestamp=discord.utils.utcnow())
    # Plain display name, never a mention — the log should not ping anyone.
    embed.add_field(name="User", value=f"{interaction.user.display_name} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="Options", value=format_options(interaction), inline=False)
    if interaction.channel:
        embed.add_field(name="Channel", value=getattr(interaction.channel, "mention", "?"), inline=False)
    embed.set_footer(text=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    await send_log(bot, interaction.guild_id, embed)


async def log_command_error(bot, interaction: discord.Interaction, error: Exception):
    """Records a command that was blocked or failed, so denied staff attempts
    and crashes are both visible in the audit channel."""
    name = interaction.command.qualified_name if interaction.command else "unknown"
    embed = discord.Embed(
        title=f"⚠️ /{name} failed",
        description=f"```{str(error)[:1000]}```",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    # Plain display name, never a mention — the log should not ping anyone.
    embed.add_field(name="User", value=f"{interaction.user.display_name} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="Options", value=format_options(interaction), inline=False)
    embed.set_footer(text=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    await send_log(bot, interaction.guild_id, embed)


# ---------------------------------------------------------------------------
# Economy reconciliation
# ---------------------------------------------------------------------------
def _snapshot_key(guild_id: int) -> str:
    return f"audit_snapshot_{guild_id}"


def _money(n) -> str:
    return f"${int(n):,}"


async def reconcile_supply(db, guild_id: int, save_snapshot: bool = True) -> dict:
    """Compares the money that exists with the money the transaction log
    accounts for since the previous audit.

    Money supply = every user's cash + bank + savings, every business account,
    and the treasury. Between two audits it should change by exactly:

        sum of logged transactions whose type creates/destroys money
        (config.AUDIT_SUPPLY_CHANGING_TYPES)
      + starting balances minted for accounts created in between

    Transfers between two pockets (bank <-> cash, /pay, /paycompany, fines and
    tax into the treasury, ...) are in the log too but cancel out, so they are
    ignored. Any remaining difference is "drift": money that appeared or
    vanished without a record, which is exactly what a dupe or a lost-update
    bug looks like from the outside.

    The first run only stores a baseline. Returns a dict with the breakdown
    and, when a baseline existed, the expected/actual/drift figures.
    """
    now = int(time.time())
    supply = await db.get_money_supply(guild_id)
    tx_max = await db.get_max_transaction_id(guild_id)

    raw = await db.get_meta(_snapshot_key(guild_id))
    previous = None
    if raw:
        try:
            previous = json.loads(raw)
        except (TypeError, ValueError):
            previous = None

    report = {"supply": supply, "now": now, "previous": previous, "drift": None}
    if previous:
        rows = await db.sum_transactions_by_type(guild_id, int(previous["tx_id"]), tx_max)
        changing = {}
        neutral = {}
        for ttype, count, total in rows:
            (changing if ttype in config.AUDIT_SUPPLY_CHANGING_TYPES else neutral)[ttype] = (count, total)
        explained = sum(total for _, total in changing.values())
        # Accounts created since the last snapshot each minted a starting
        # balance without a transaction. Counted from the row count stored in
        # the snapshot (older snapshots without it fall back to created_at).
        if "users" in previous:
            new_users = max(0, int(supply["users"]) - int(previous["users"]))
        else:
            new_users = await db.count_users_created_between(guild_id, int(previous["ts"]), now)
        minted = new_users * (int(config.STARTING_CASH) + int(config.STARTING_BANK))
        expected = explained + minted
        actual = int(supply["total"]) - int(previous["supply"])
        report.update({
            "changing": changing,
            "neutral": neutral,
            "explained": explained,
            "new_users": new_users,
            "minted": minted,
            "expected_delta": expected,
            "actual_delta": actual,
            "drift": actual - expected,
        })

    if save_snapshot:
        await db.set_meta(
            _snapshot_key(guild_id),
            json.dumps({"supply": int(supply["total"]), "tx_id": tx_max, "ts": now, "users": int(supply["users"])}),
        )
    return report


async def find_suspicious_patterns(db, guild_id: int, lookback_secs: int) -> list:
    """Returns a list of human-readable findings (empty = nothing odd)."""
    now = int(time.time())
    since = now - lookback_secs
    findings = []

    # Pattern 1: repeatedly filling orders at the per-order cap.
    bursts = await db.find_cap_trade_bursts(
        guild_id, since, int(config.AUDIT_CAP_TRADE_COUNT), int(config.AUDIT_CAP_TRADE_WINDOW_MINUTES * 60)
    )
    for user_id, business_id, name, count, first_ts, last_ts in bursts:
        span = max(1, last_ts - first_ts)
        findings.append(
            f"📈 <@{user_id}> filled **{count}** max-size orders "
            f"({config.STOCK_MAX_TRADE_PCT_OF_SHARES * 100:.0f}% of shares each) on **{name}** "
            f"within {max(1, span // 60)} min (<t:{first_ts}:R> → <t:{last_ts}:R>)."
        )

    # Pattern 2: a company paid by thin accounts just before an insider dumps.
    sells = await db.find_large_insider_sells(guild_id, since, float(config.AUDIT_LARGE_SELL_PCT))
    window = int(config.AUDIT_PAYMENT_TO_SELL_WINDOW_HOURS * 3600)
    seen = set()
    for user_id, business_id, name, shares, amount, ts, company_id in sells:
        if company_id is None or (user_id, company_id, ts // window) in seen:
            continue
        seen.add((user_id, company_id, ts // window))
        payers = await db.get_company_payers_between(company_id, ts - window, ts)
        thin = []
        for payer_id, payments, total in payers:
            if payer_id == user_id:
                continue
            tx_count, first_seen = await db.get_account_activity(payer_id, guild_id)
            is_new = first_seen and (ts - first_seen) < config.AUDIT_NEW_ACCOUNT_AGE_HOURS * 3600
            if tx_count < config.AUDIT_LOW_ACTIVITY_TX_COUNT or is_new:
                thin.append((payer_id, payments, total))
        if len(thin) >= int(config.AUDIT_SUSPICIOUS_PAYMENT_COUNT):
            payer_text = ", ".join(f"<@{p}> ({_money(t)} in {n})" for p, n, t in thin[:5])
            findings.append(
                f"🏢 <@{user_id}> sold **{shares:,}** shares of **{name}** for {_money(amount)} <t:{ts}:R>, "
                f"after **{len(thin)}** low-activity/new accounts paid the linked business in the previous "
                f"{config.AUDIT_PAYMENT_TO_SELL_WINDOW_HOURS:g}h: {payer_text}."
            )
    return findings


def build_audit_embed(report: dict, findings: list, trigger: str) -> discord.Embed:
    supply = report["supply"]
    drift = report.get("drift")
    tolerance = int(config.AUDIT_DRIFT_TOLERANCE)
    flagged = (drift is not None and abs(drift) > tolerance) or bool(findings)

    embed = discord.Embed(
        title="🧮 Economy audit" + (" — ⚠️ attention needed" if flagged else " — ✅ clean"),
        color=discord.Color.red() if flagged else discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name=f"Money supply: {_money(supply['total'])}",
        value=(
            f"Cash {_money(supply['cash'])} · Bank {_money(supply['bank'])} · Savings {_money(supply['savings'])}\n"
            f"Business accounts {_money(supply['companies'])} · Treasury {_money(supply['treasury'])}\n"
            f"{supply['users']:,} accounts"
        ),
        inline=False,
    )

    previous = report.get("previous")
    if not previous:
        embed.add_field(
            name="Reconciliation",
            value="First run — baseline recorded. The next audit will compare against it.",
            inline=False,
        )
    else:
        status = "✅ balanced" if abs(drift) <= tolerance else f"⚠️ **{_money(drift)} unexplained**"
        changing = report.get("changing", {})
        lines = [
            f"Since <t:{int(previous['ts'])}:R>: supply moved **{_money(report['actual_delta'])}**, "
            f"the log explains **{_money(report['expected_delta'])}** → {status}",
        ]
        if changing:
            detail = ", ".join(f"{t} {_money(total)} ({n})" for t, (n, total) in sorted(changing.items()))
            lines.append(f"Minted/burned: {detail}"[:900])
        if report.get("new_users"):
            lines.append(f"New accounts: {report['new_users']} × {_money(config.STARTING_CASH + config.STARTING_BANK)} = {_money(report['minted'])}")
        if abs(drift) > tolerance:
            lines.append(
                "Drift means money changed without a matching transaction record. Known unlogged sources: "
                "`/eco-admin reset` (balance reset), UnbelievaBoat migration in *replace* mode, and any "
                "manual database edit. If none of those happened, suspect a dupe or lost-update bug."
            )
        embed.add_field(name="Reconciliation", value="\n".join(lines)[:1024], inline=False)

    if findings:
        text = "\n".join(f"• {f}" for f in findings)
        embed.add_field(name=f"Suspicious patterns ({len(findings)})", value=text[:1024], inline=False)
    else:
        embed.add_field(
            name="Suspicious patterns",
            value=f"None in the last {config.AUDIT_LOOKBACK_HOURS:g}h.",
            inline=False,
        )
    embed.set_footer(text=f"Triggered by {trigger}")
    return embed


async def run_reconciliation(bot, guild_id: int, trigger: str = "schedule", post: bool = True):
    """Runs the full audit for one guild. Returns (embed, flagged). When
    `post` is True and something was flagged, the embed is also sent to the
    guild's audit log channel."""
    db = bot.db
    report = await reconcile_supply(db, guild_id)
    findings = await find_suspicious_patterns(db, guild_id, int(config.AUDIT_LOOKBACK_HOURS * 3600))
    embed = build_audit_embed(report, findings, trigger)
    drift = report.get("drift")
    flagged = (drift is not None and abs(drift) > int(config.AUDIT_DRIFT_TOLERANCE)) or bool(findings)
    if flagged:
        log.warning(
            "Economy audit flagged guild %s: drift=%s findings=%d", guild_id, drift, len(findings)
        )
    if post and flagged:
        await send_log(bot, guild_id, embed)
    return embed, flagged
