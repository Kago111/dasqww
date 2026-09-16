import asyncio
import json
import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import audit
from cogs import persistence
import channels as channel_locks
import config
import stockimpact
import whitelist
from amounts import parse_amount, parse_count, parse_price
from cogs.economy import fmt, credit_rating, send_error
from cogs.vehicles import generate_plate

log = logging.getLogger("beamng-eco-bot.admin")

# Friendly names for the tables a full economy reset clears, used in the
# confirmation preview and in the report afterwards.
RESET_TABLE_LABELS = {
    "users": "Player accounts (cash, bank, savings, credit, jobs)",
    "vehicles": "Registered vehicles",
    "fines": "Fines",
    "loans": "Loans",
    "loan_requests": "Loan requests",
    "transactions": "Transaction history entries",
    "treasury": "Server treasury",
    "businesses": "Stock market listings",
    "stock_holdings": "Shareholdings",
    "stock_events": "Stock history events",
    "companies": "Registered businesses",
    "company_staff": "Business staff records",
    "company_ledger": "Business ledger entries",
    "stock_trades": "Stock trade records",
    "keyed_cooldowns": "Per-business payment cooldowns",
    "guild_settings": "Server settings (log / market / restricted channels, whitelist roles)",
}


def may_reset_economy(member: discord.Member, guild: discord.Guild) -> bool:
    """Who may wipe the entire server economy. Deliberately stricter than
    is_eco_staff(): an economy staff role is not enough, and neither is the
    Administrator permission, because this cannot be undone. The server owner
    always qualifies (so the list can never lock the server out), plus anyone
    listed in config.ECONOMY_RESET_USER_IDS."""
    if guild.owner_id == member.id:
        return True
    return member.id in set(getattr(config, "ECONOMY_RESET_USER_IDS", []) or [])


class EconomyResetView(discord.ui.View):
    """Second gate on the economy reset: typing the confirmation phrase gets you
    this button, and only the person who ran the command can press it."""

    def __init__(self, cog: "Admin", author_id: int, reset_settings: bool):
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.reset_settings = reset_settings

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Yes \u2014 erase everything", style=discord.ButtonStyle.danger, emoji="\u26a0\ufe0f")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(
            content="\u23f3 Backing up and resetting the economy...", embed=None, view=self
        )
        try:
            embed = await self.cog.perform_reset(interaction, self.reset_settings)
        except Exception as exc:
            log.exception("Economy reset failed")
            await interaction.edit_original_response(
                content=f"\u274c The reset failed and nothing was deleted: {exc}", embed=None, view=None
            )
            self.stop()
            return

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        # Announce it publicly too: players whose balances just vanished deserve
        # to know why without having to ask.
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(
            content="\u2705 Cancelled \u2014 nothing was deleted.", embed=None, view=self
        )
        self.stop()


class CompanyDeleteView(discord.ui.View):
    """Confirmation gate for /eco-admin company delete.

    Deleting a registered business destroys its ledger and its staff roster, so
    it is never done on a single click of a command — and only the admin who ran
    the command can confirm it.
    """

    def __init__(self, cog: "Admin", author_id: int, company, payout: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.company = company
        self.payout = payout

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Delete this business", style=discord.ButtonStyle.danger, emoji="\U0001f5d1\ufe0f")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(content="\u23f3 Deleting...", embed=None, view=self)
        try:
            embed = await self.cog.perform_company_delete(interaction, self.company, self.payout)
        except Exception as exc:
            log.exception("Company deletion failed")
            await interaction.edit_original_response(
                content=f"\u274c Deletion failed and nothing was removed: {exc}", embed=None, view=None
            )
            self.stop()
            return

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        # The confirmation is ephemeral, but the closure isn't private: staff
        # and employees of the business should see it happen rather than find
        # out when their commands stop working.
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(
            content="\u2705 Cancelled \u2014 the business was not deleted.", embed=None, view=self
        )
        self.stop()


def is_eco_staff(member: discord.Member, guild: discord.Guild) -> bool:
    """True if the member may use /eco-admin commands: they hold one of
    config.ECO_ADMIN_ROLE_IDS, they own the server (so a bad role list can never
    lock the server out), or they are an Administrator while
    config.ECO_ADMIN_ALLOW_ADMINISTRATOR is True."""
    if guild.owner_id == member.id:
        return True
    if config.ECO_ADMIN_ALLOW_ADMINISTRATOR and member.guild_permissions.administrator:
        return True
    return any(r.id in config.ECO_ADMIN_ROLE_IDS for r in member.roles)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reconciliation_loop.start()

    def cog_unload(self):
        self.reconciliation_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    # Scheduled economy audit (audit.run_reconciliation). Wakes every few
    # minutes but only runs once per AUDIT_RECONCILE_INTERVAL_HOURS, measured
    # from a timestamp in the database — same trick as the market tick, so a
    # host that restarts the bot constantly can't make it run on every boot.
    # Only posts to the log channel when something is flagged.
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5)
    async def reconciliation_loop(self):
        if not config.AUDIT_RECONCILE_ENABLED:
            return
        now = int(time.time())
        last = int(await self.db.get_meta("last_reconcile_audit", 0) or 0)
        interval = max(1, int(config.AUDIT_RECONCILE_INTERVAL_HOURS * 3600))
        if last and now - last < interval:
            return
        await self.db.set_meta("last_reconcile_audit", now)
        for guild_id in await self.db.get_all_guild_ids():
            try:
                await audit.run_reconciliation(self.bot, guild_id, trigger="schedule", post=True)
            except Exception:
                log.exception("Scheduled economy audit failed for guild %s", guild_id)

    @reconciliation_loop.before_loop
    async def before_reconciliation_loop(self):
        await self.bot.wait_until_ready()
        # Both this loop and cogs.persistence's checkpoint loop fire their
        # first tick the moment the bot is ready. Neither is harmful alone,
        # but landing on the exact same instant is what makes a TRUNCATE
        # checkpoint collide with an in-flight audit query. A few seconds of
        # separation costs nothing here and avoids it in the common case;
        # database.checkpoint() also tolerates the collision if it still happens.
        await asyncio.sleep(5)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every command in this cog, so all current and future
        /eco-admin subcommands are gated on the staff roles automatically."""
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if is_eco_staff(interaction.user, interaction.guild):
            return True
        # A co-owner listed for the economy reset gets through this gate even
        # without a staff role; the command itself re-checks who they are.
        if may_reset_economy(interaction.user, interaction.guild):
            return True
        raise app_commands.CheckFailure(
            "You need an economy staff role to use /eco-admin commands."
        )

    # ------------------------------------------------------------------ #
    # Autocomplete helpers so staff pick existing records from a dropdown.
    # ------------------------------------------------------------------ #
    async def listed_autocomplete(self, interaction: discord.Interaction, current: str):
        """Businesses listed on the stock market."""
        businesses = await self.db.list_businesses(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=f"{name} — ${price:,.2f}"[:100], value=name)
            for _, name, _, price, *_ in businesses
            if not current or current in name.lower()
        ][:25]

    async def company_autocomplete(self, interaction: discord.Interaction, current: str):
        """Real businesses (companies), not stock-market listings."""
        companies = await self.db.list_companies(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=f"{name} — {fmt(revenue)} all-time"[:100], value=name)
            for _, name, _, _, revenue, *_ in companies
            if not current or current in name.lower()
        ][:25]

    admin_group = app_commands.Group(
        name="eco-admin",
        description="Server admin economy management commands.",
        # Hidden from everyone by default. Discord then only shows /eco-admin to
        # members you grant it to under Server Settings -> Integrations, and the
        # is_eco_staff() check below enforces config.ECO_ADMIN_ROLE_IDS server-side.
        default_permissions=discord.Permissions.none(),
    )

    business_group = app_commands.Group(
        name="business",
        description="Admin-only stock market business management.",
        parent=admin_group,
    )

    company_group = app_commands.Group(
        name="company",
        description="Admin-only real business management (not stock market).",
        parent=admin_group,
    )

    channels_group = app_commands.Group(
        name="channels",
        description="Restrict stock and business commands to specific channels.",
        parent=admin_group,
    )

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Control which roles may use the bot and have an economy account.",
        parent=admin_group,
    )

    db_group = app_commands.Group(
        name="db",
        description="Database safety — run these around every bot update.",
        parent=admin_group,
    )

    # ------------------------------------------------------------------ #
    # Database / bot-update safety.
    #
    # The workflow these three commands exist to make foolproof:
    #
    #   BEFORE an update:  /eco-admin db prepare-update
    #                      -> flushes everything to economy.db, uploads a
    #                         verified copy, and remembers the exact totals
    #   AFTER  an update:  /eco-admin db verify-update
    #                      -> compares the running economy to those totals and
    #                         says plainly whether anything went missing
    # ------------------------------------------------------------------ #
    @db_group.command(
        name="prepare-update",
        description="Run BEFORE a bot update: saves a verified copy of the economy and records its totals.",
    )
    async def db_prepare_update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        pending = self.db.wal_size()
        fingerprint = await self.db.economy_fingerprint()
        fingerprint["saved_at"] = int(time.time())
        fingerprint["saved_by"] = interaction.user.id
        await self.db.set_meta(persistence.PRE_UPDATE_KEY, json.dumps(fingerprint))
        await self.db.set_meta(persistence.LAST_RUN_KEY, json.dumps(fingerprint))

        backup_cog = self.bot.get_cog("Backup")
        if backup_cog is not None:
            backup_result = await backup_cog.send_backup(
                f"pre-update by {interaction.user.display_name}"
            )
        else:
            backup_result = "Backups are not loaded — take a copy of economy.db by hand."

        embed = discord.Embed(
            title="\U0001f4be Ready for the update",
            description=(
                "Everything is written to `economy.db` and its totals are recorded. "
                "Copy the file now — nothing is left behind."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Economy snapshot",
            value=(
                f"Players: **{fingerprint['players']:,}**\n"
                f"Money in the economy: **{fmt(fingerprint['money_supply'])}**\n"
                f"Shares held: **{fingerprint['shares_held']:,}** "
                f"across **{fingerprint['shareholders']:,}** holdings\n"
                f"Transactions logged: **{fingerprint['transactions']:,}**"
            ),
            inline=False,
        )
        embed.add_field(name="Backup", value=backup_result, inline=False)
        if pending:
            embed.add_field(
                name="Unsaved writes flushed",
                value=(
                    f"{pending / 1024:.0f} KB was still in the write-ahead log and has now been "
                    "folded into `economy.db`. That is exactly the data a straight copy would "
                    "have lost."
                ),
                inline=False,
            )
        embed.add_field(
            name="Now do this",
            value=(
                "1. **Stop the bot** in your host panel.\n"
                "2. Download `economy.db` — and `economy.db-wal` / `economy.db-shm` too if "
                "they are there.\n"
                "3. Replace the bot files with the new version.\n"
                "4. Upload `economy.db` back, into the same folder.\n"
                "5. Start the bot, then run `/eco-admin db verify-update`."
            ),
            inline=False,
        )
        embed.set_footer(text="See UPDATING.md in the bot files for the long version.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @db_group.command(
        name="verify-update",
        description="Run AFTER a bot update: checks that nothing was lost from the economy.",
    )
    async def db_verify_update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        raw = await self.db.get_meta(persistence.PRE_UPDATE_KEY)
        if not raw:
            await interaction.followup.send(
                "\u2139\ufe0f Nothing to compare against. Run `/eco-admin db prepare-update` "
                "**before** the next update and this command will be able to prove afterwards "
                "that the economy came through intact.",
                ephemeral=True,
            )
            return

        before = json.loads(raw)
        after = await self.db.economy_fingerprint()
        result = persistence.compare_fingerprints(before, after)
        when = before.get("saved_at", 0)

        if result["verdict"] == "lost":
            embed = discord.Embed(
                title="\U0001f6a8 Data is missing",
                description=(
                    "The economy is **smaller** than it was before the update. An older copy of "
                    "`economy.db` was almost certainly uploaded.\n\n**Restore the most recent "
                    "backup from the backup channel and restart the bot** before players carry on."
                ),
                color=discord.Color.red(),
            )
        elif result["verdict"] == "warn":
            embed = discord.Embed(
                title="\u26a0\ufe0f Everything is present, but money moved",
                description=(
                    "No records were lost. Some balances are lower than before, which is normal "
                    "if players have been spending since the snapshot — check the list below "
                    "looks like ordinary play."
                ),
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title="\u2705 The economy came through intact",
                description="Every account, portfolio, business and record survived the update.",
                color=discord.Color.green(),
            )

        if when:
            embed.description += f"\n\nCompared against the snapshot from <t:{when}:F>."

        if result["losses"]:
            embed.add_field(
                name="\U0001f6a8 Missing",
                value="\n".join(
                    f"**{e['label']}** — was {persistence.fmt_field(e['key'], e['before'])}, "
                    f"now {persistence.fmt_field(e['key'], e['after'])} "
                    f"({persistence.fmt_field(e['key'], abs(e['delta']))} gone)"
                    for e in result["losses"][:10]
                ),
                inline=False,
            )

        other = [c for c in result["changes"] if c not in result["losses"]]
        if other:
            embed.add_field(
                name="Changed since the snapshot (normal play)",
                value="\n".join(
                    "{}: {} \u2192 {} ({}{})".format(
                        e["label"],
                        persistence.fmt_field(e["key"], e["before"]),
                        persistence.fmt_field(e["key"], e["after"]),
                        "+" if e["delta"] > 0 else "\u2212",
                        persistence.fmt_field(e["key"], abs(e["delta"])),
                    )
                    for e in other[:12]
                )[:1024],
                inline=False,
            )
        elif not result["losses"]:
            embed.add_field(
                name="Changed since the snapshot",
                value="Nothing at all — the database is identical.",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @db_group.command(
        name="status",
        description="Database health: where it lives, how current the file is, and when it was last backed up.",
    )
    async def db_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        path = os.path.abspath(self.db.path)
        wal = self.db.wal_size()
        folded = await self.db.checkpoint()
        size = os.path.getsize(self.db.path) if os.path.exists(self.db.path) else 0
        fingerprint = await self.db.economy_fingerprint()
        last_backup = int(await self.db.get_meta("last_backup", 0) or 0)

        on_disposable_disk = not any(
            path.startswith(prefix) for prefix in ("/data", "/persistent", "/mnt", "/var/lib/data")
        )

        embed = discord.Embed(title="\U0001f5c4\ufe0f Database status", color=discord.Color.blurple())
        embed.add_field(name="File", value=f"`{path}`\n{size / 1024:.0f} KB", inline=False)
        embed.add_field(
            name="Up to date on disk",
            value=(
                f"\u2705 Yes — {folded / 1024:.0f} KB was just flushed, the file is current."
                if folded else
                "\u2705 Yes — nothing was waiting to be written."
            ) + (
                f"\n(WAL held {wal / 1024:.0f} KB a moment ago; it is checkpointed every "
                f"{int(getattr(config, 'CHECKPOINT_INTERVAL_SECONDS', 30))}s.)" if wal else ""
            ),
            inline=False,
        )
        embed.add_field(
            name="Contents",
            value=(
                f"{fingerprint['players']:,} players \u00b7 {fmt(fingerprint['money_supply'])} in circulation\n"
                f"{fingerprint['shares_held']:,} shares held \u00b7 {fingerprint['listings']:,} listings\n"
                f"{fingerprint['transactions']:,} transactions \u00b7 {fingerprint['stock_trades']:,} trades"
            ),
            inline=False,
        )
        embed.add_field(
            name="Last backup",
            value=f"<t:{last_backup}:R>" if last_backup else "Never — set `BACKUP_CHANNEL_ID`.",
            inline=False,
        )
        if on_disposable_disk:
            embed.add_field(
                name="\u26a0\ufe0f Stored inside the bot folder",
                value=(
                    "The database sits in the same folder as the bot's code, so it is in the "
                    "firing line every time you replace the files. Set `DATABASE_PATH` to a "
                    "location outside that folder and the economy stops being something you "
                    "have to carry across updates by hand."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Whitelist
    # ------------------------------------------------------------------ #
    @whitelist_group.command(name="show", description="See which roles are allowed to use the bot.")
    async def whitelist_show(self, interaction: discord.Interaction):
        stored = await self.db.get_channel_lock(interaction.guild_id, whitelist.COLUMN)
        from_config = whitelist.config_roles()

        if not whitelist.enabled():
            where = (
                "**Off** — everyone in the server can use the bot.\n"
                "Set `WHITELIST_ENABLED = True` in `config.py` to turn it on."
            )
        elif stored:
            where = whitelist.describe(stored) + "\n*(set here in Discord)*"
        elif from_config:
            where = whitelist.describe(from_config) + "\n*(from config.py)*"
        else:
            where = (
                "⚠️ **Nobody.** The whitelist is on but no role has been set, so only staff "
                "can use the bot. Add one with `/eco-admin whitelist add`."
            )

        embed = discord.Embed(
            title="🔒 Bot whitelist",
            description=(
                "Only members with one of these roles can run the bot's commands. "
                "Everyone else gets a private note and never receives an economy account."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Allowed roles", value=where, inline=False)
        bypass = "Yes" if getattr(config, "WHITELIST_STAFF_BYPASS", True) else "No (owner only)"
        embed.set_footer(text=f"Economy staff and admins bypass the whitelist: {bypass}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @whitelist_group.command(name="add", description="Allow a role to use the bot.")
    @app_commands.describe(role="Role whose members may use the bot")
    async def whitelist_add(self, interaction: discord.Interaction, role: discord.Role):
        current = await self.db.get_channel_lock(interaction.guild_id, whitelist.COLUMN)
        if not current:
            # Seed from config.py so adding one role doesn't silently drop any
            # already listed there.
            current = whitelist.config_roles()
        if role.id in current:
            await send_error(interaction, f"{role.mention} is already whitelisted.")
            return

        updated = await self.db.set_channel_lock(interaction.guild_id, whitelist.COLUMN, current + [role.id])
        note = (
            ""
            if whitelist.enabled()
            else "\n\n⚠️ The whitelist is currently **off** (`WHITELIST_ENABLED = False` in `config.py`), "
            "so this list isn't being enforced yet."
        )
        await interaction.response.send_message(
            f"🔒 Whitelisted {role.mention}. Members who may use the bot: "
            f"{whitelist.describe(updated)}.{note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @whitelist_group.command(name="remove", description="Stop allowing a role to use the bot.")
    @app_commands.describe(role="Role to remove from the whitelist")
    async def whitelist_remove(self, interaction: discord.Interaction, role: discord.Role):
        current = await self.db.get_channel_lock(interaction.guild_id, whitelist.COLUMN)
        if not current:
            current = whitelist.config_roles()
        if role.id not in current:
            await send_error(interaction, f"{role.mention} isn't on the whitelist.")
            return

        updated = await self.db.set_channel_lock(
            interaction.guild_id, whitelist.COLUMN, [r for r in current if r != role.id]
        )
        if updated:
            message = f"Removed {role.mention}. Members who may use the bot: {whitelist.describe(updated)}."
        else:
            message = (
                f"Removed {role.mention}. **No roles are left on the whitelist**, so only economy staff "
                "can use the bot. Add one with `/eco-admin whitelist add`."
            )
        await interaction.response.send_message(message, allowed_mentions=discord.AllowedMentions.none())

    @whitelist_group.command(name="clear", description="Clear the in-Discord whitelist for this server.")
    async def whitelist_clear(self, interaction: discord.Interaction):
        await self.db.set_channel_lock(interaction.guild_id, whitelist.COLUMN, [])
        from_config = whitelist.config_roles()
        if from_config:
            await interaction.response.send_message(
                "Cleared the in-Discord whitelist. It now falls back to the roles set in `config.py`: "
                f"{whitelist.describe(from_config)}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif whitelist.enabled():
            await interaction.response.send_message(
                "Cleared the whitelist. The whitelist is still **on** and now lists no roles, so only "
                "economy staff can use the bot. Add a role with `/eco-admin whitelist add`, or set "
                "`WHITELIST_ENABLED = False` in `config.py` to open the bot to everyone."
            )
        else:
            await interaction.response.send_message(
                "Cleared the whitelist. The whitelist is off, so everyone can use the bot."
            )

    # ------------------------------------------------------------------ #
    # Channel restrictions
    # ------------------------------------------------------------------ #
    AREA_CHOICES = [
        app_commands.Choice(name="Stock market commands", value="stock"),
        app_commands.Choice(name="Business info commands", value="business"),
    ]

    @channels_group.command(name="show", description="See which channels the stock and business commands are locked to.")
    async def channels_show(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📍 Channel restrictions",
            description=(
                "These commands only work in the channels shown (threads inside them count too). "
                "An area with no channels set works everywhere."
            ),
            color=discord.Color.blurple(),
        )
        for area, info in channel_locks.AREAS.items():
            stored = await self.db.get_channel_lock(interaction.guild_id, info["column"])
            from_config = channel_locks.config_channels(area)
            if stored:
                where = channel_locks.describe(area, stored) + "\n*(set here in Discord)*"
            elif from_config:
                where = channel_locks.describe(area, from_config) + "\n*(from config.py)*"
            else:
                where = "**Anywhere** — not restricted"
            embed.add_field(
                name=f"{info['emoji']} {info['label']}",
                value=f"{info['commands']}\n→ {where}",
                inline=False,
            )
        bypass = "Yes" if getattr(config, "CHANNEL_LOCK_STAFF_BYPASS", True) else "No"
        embed.set_footer(text=f"Economy staff can use them anywhere: {bypass}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @channels_group.command(name="add", description="Allow an area's commands in a channel.")
    @app_commands.describe(area="Which commands to restrict", channel="Channel to allow them in")
    @app_commands.choices(area=AREA_CHOICES)
    async def channels_add(
        self, interaction: discord.Interaction, area: app_commands.Choice[str], channel: discord.TextChannel
    ):
        info = channel_locks.AREAS[area.value]
        current = await self.db.get_channel_lock(interaction.guild_id, info["column"])
        if not current:
            # First channel added for this area: seed from config.py so adding
            # one channel doesn't silently drop any already listed there.
            current = channel_locks.config_channels(area.value)
        if channel.id in current:
            await send_error(
                interaction, f"{channel.mention} is already allowed for {info['label'].lower()} commands."
            )
            return

        updated = await self.db.set_channel_lock(interaction.guild_id, info["column"], current + [channel.id])
        await interaction.response.send_message(
            f"{info['emoji']} {info['label']} commands ({info['commands']}) can now be used in "
            f"{channel_locks.describe(area.value, updated)} — and nowhere else."
        )

    @channels_group.command(name="remove", description="Stop allowing an area's commands in a channel.")
    @app_commands.describe(area="Which commands to restrict", channel="Channel to remove")
    @app_commands.choices(area=AREA_CHOICES)
    async def channels_remove(
        self, interaction: discord.Interaction, area: app_commands.Choice[str], channel: discord.TextChannel
    ):
        info = channel_locks.AREAS[area.value]
        current = await self.db.get_channel_lock(interaction.guild_id, info["column"])
        if not current:
            current = channel_locks.config_channels(area.value)
        if channel.id not in current:
            await send_error(
                interaction, f"{channel.mention} isn't on the list for {info['label'].lower()} commands."
            )
            return

        updated = await self.db.set_channel_lock(
            interaction.guild_id, info["column"], [c for c in current if c != channel.id]
        )
        if updated:
            await interaction.response.send_message(
                f"Removed {channel.mention}. {info['label']} commands now work in "
                f"{channel_locks.describe(area.value, updated)}."
            )
        else:
            await interaction.response.send_message(
                f"Removed {channel.mention}. No channels are left on the list, so {info['label'].lower()} "
                "commands now work **anywhere**. Add one with `/eco-admin channels add`."
            )

    @channels_group.command(name="clear", description="Let an area's commands be used in any channel again.")
    @app_commands.describe(area="Which commands to unrestrict")
    @app_commands.choices(area=AREA_CHOICES)
    async def channels_clear(self, interaction: discord.Interaction, area: app_commands.Choice[str]):
        info = channel_locks.AREAS[area.value]
        await self.db.set_channel_lock(interaction.guild_id, info["column"], [])
        from_config = channel_locks.config_channels(area.value)
        if from_config:
            # Be honest rather than claiming it is now unrestricted: with the
            # in-Discord list empty, the config.py list takes over again.
            await interaction.response.send_message(
                f"Cleared the in-Discord list for {info['label'].lower()} commands. They now fall back to the "
                f"channels set in `config.py`: {channel_locks.describe(area.value, from_config)}.\n"
                "Empty that list in the file to remove the restriction entirely."
            )
        else:
            await interaction.response.send_message(
                f"{info['label']} commands ({info['commands']}) can now be used in **any** channel."
            )

    # ------------------------------------------------------------------ #
    @admin_group.command(name="give", description="Give a player cash or bank funds.")
    @app_commands.describe(user="Recipient", amount="Amount to give (e.g. 500, 10k, 1.5m)", account="cash or bank")
    @app_commands.choices(account=[
        app_commands.Choice(name="Cash", value="cash"),
        app_commands.Choice(name="Bank", value="bank"),
    ])
    async def give(self, interaction: discord.Interaction, user: discord.Member, amount: str, account: app_commands.Choice[str]):
        amount, error = parse_amount(amount, noun="amount")
        if error:
            await send_error(interaction, error)
            return
        if account.value == "cash":
            await self.db.add_cash(user.id, interaction.guild_id, amount)
        else:
            await self.db.add_bank(user.id, interaction.guild_id, amount)
        await self.db.log_transaction(user.id, interaction.guild_id, "admin_give", amount, f"By {interaction.user.display_name}")
        await interaction.response.send_message(f"Gave {user.mention} {fmt(amount)} ({account.value}).")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="take", description="Remove cash or bank funds from a player.")
    @app_commands.describe(user="Target", amount="Amount to remove (e.g. 500, 10k, 1.5m)", account="cash or bank")
    @app_commands.choices(account=[
        app_commands.Choice(name="Cash", value="cash"),
        app_commands.Choice(name="Bank", value="bank"),
    ])
    async def take(self, interaction: discord.Interaction, user: discord.Member, amount: str, account: app_commands.Choice[str]):
        amount, error = parse_amount(amount, noun="amount")
        if error:
            await send_error(interaction, error)
            return
        if account.value == "cash":
            await self.db.add_cash(user.id, interaction.guild_id, -amount)
        else:
            await self.db.add_bank(user.id, interaction.guild_id, -amount)
        await self.db.log_transaction(user.id, interaction.guild_id, "admin_take", -amount, f"By {interaction.user.display_name}")
        await interaction.response.send_message(f"Removed {fmt(amount)} ({account.value}) from {user.mention}.")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="reset", description="Reset a player's cash and bank to the default starting amounts.")
    @app_commands.describe(user="Target player")
    async def reset(self, interaction: discord.Interaction, user: discord.Member):
        await self.db.set_balance(user.id, interaction.guild_id, config.STARTING_CASH, config.STARTING_BANK)
        await interaction.response.send_message(f"Reset {user.mention}'s balance to the server defaults.")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="setjob", description="Force-set a player's job.")
    @app_commands.describe(user="Target player", job="Job to assign")
    @app_commands.choices(job=[app_commands.Choice(name=j, value=j) for j in config.JOBS.keys()])
    async def setjob(self, interaction: discord.Interaction, user: discord.Member, job: app_commands.Choice[str]):
        await self.db.set_job(user.id, interaction.guild_id, job.value)
        await interaction.response.send_message(f"Set {user.mention}'s job to **{job.value}**.")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="credit-adjust", description="Raise or lower a player's credit score.")
    @app_commands.describe(user="Target player", delta="Points to add (use a negative number to subtract)")
    async def credit_adjust(self, interaction: discord.Interaction, user: discord.Member, delta: app_commands.Range[int, -550, 550]):
        if delta == 0:
            await send_error(interaction, "Delta must be non-zero.")
            return
        new_score = await self.db.adjust_credit_score(
            user.id, interaction.guild_id, delta, config.MIN_CREDIT_SCORE, config.MAX_CREDIT_SCORE
        )
        await interaction.response.send_message(
            f"{user.mention}'s credit score is now **{new_score}** ({credit_rating(new_score)}), change {delta:+d}."
        )

    # ------------------------------------------------------------------ #
    @admin_group.command(name="givevehicle", description="Register a vehicle in a player's garage.")
    @app_commands.describe(
        user="Owner of the vehicle",
        name="Vehicle name, e.g. Gavril D-Series",
        value="Vehicle value in dollars (e.g. 25000, 25k)",
        category="Vehicle category",
    )
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in config.VEHICLE_CATEGORIES])
    async def givevehicle(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        name: app_commands.Range[str, 1, 60],
        value: str,
        category: app_commands.Choice[str],
    ):
        value, error = parse_amount(value, minimum=1, maximum=100_000_000, noun="vehicle value")
        if error:
            await send_error(interaction, error)
            return
        plate = generate_plate()
        await self.db.add_vehicle(user.id, interaction.guild_id, name.strip(), category.value, value, plate)
        await interaction.response.send_message(
            f"\U0001f697 Registered a **{name.strip()}** ({category.value}, {fmt(value)}) to {user.mention}. Plate: `{plate}`"
        )

    @admin_group.command(name="removevehicle", description="Remove a vehicle from a player's garage by ID.")
    @app_commands.describe(vehicle_id="Vehicle ID (shown in /garage)")
    async def removevehicle(self, interaction: discord.Interaction, vehicle_id: int):
        vehicle = await self.db.get_vehicle(vehicle_id, interaction.guild_id)
        if not vehicle:
            await send_error(interaction, "No vehicle with that ID on this server.")
            return
        _, owner_id, name, _, _, _, _, plate = vehicle
        await self.db.remove_vehicle(vehicle_id)
        await interaction.response.send_message(f"Removed **{name}** ({plate}) from <@{owner_id}>'s garage.")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="treasury", description="View the server's economy treasury balance (from fines and bounties).")
    async def treasury(self, interaction: discord.Interaction):
        balance = await self.db.get_treasury(interaction.guild_id)
        await interaction.response.send_message(f"Server treasury balance: **{fmt(balance)}**")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="market-channel", description="Set the channel where automatic market updates are posted.")
    @app_commands.describe(channel="The channel for periodic market update embeds")
    async def market_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_market_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"Market updates will now be posted in {channel.mention}.")

    # ------------------------------------------------------------------ #
    @admin_group.command(name="log-channel", description="Set the channel where all bot activity is logged.")
    @app_commands.describe(channel="The channel to post audit logs in (leave blank to disable logging)")
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await self.db.set_log_channel(interaction.guild_id, channel.id if channel else None)
        if channel is None:
            await interaction.response.send_message("Audit logging disabled.")
            return

        await interaction.response.send_message(
            f"All bot activity will now be logged in {channel.mention}."
        )
        # Confirm the bot can actually post there, rather than failing silently later.
        try:
            await channel.send(
                embed=discord.Embed(
                    title="📋 Audit logging enabled",
                    description=f"Set up by {interaction.user.display_name}. All command activity will appear here.",
                    color=discord.Color.green(),
                )
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ I can't send messages in {channel.mention}. Grant me **View Channel**, "
                "**Send Messages** and **Embed Links** there or logs will be dropped.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------ #
    # Business / stock market administration (ADMIN ONLY)
    # ------------------------------------------------------------------ #
    @business_group.command(name="create", description="[Admin] List a new business on the stock market — the owner can run/pump their own stock.")
    @app_commands.describe(
        name="Business name (must be unique)",
        initial_price="Starting price per share (e.g. 12.50, 1.5k)",
        total_shares="Total number of shares to issue (e.g. 10000, 10k)",
        owner="Optional player who owns/runs this business",
        founder_percent="% of total shares given free to the owner as a founder stake (0-100). Defaults to the server setting.",
    )
    async def business_create(
        self,
        interaction: discord.Interaction,
        name: str,
        initial_price: str,
        total_shares: str,
        owner: discord.Member = None,
        founder_percent: app_commands.Range[float, 0.0, 100.0] = None,
    ):
        initial_price, error = parse_price(initial_price, minimum=0.5, maximum=100_000.0, noun="share price")
        if error:
            await send_error(interaction, error)
            return
        total_shares, error = parse_count(total_shares, minimum=1, maximum=10_000_000, noun="share count")
        if error:
            await send_error(interaction, error)
            return
        if len(name) > config.MAX_BUSINESS_NAME_LENGTH:
            await interaction.response.send_message(
                f"Business name must be {config.MAX_BUSINESS_NAME_LENGTH} characters or fewer.", ephemeral=True
            )
            return

        existing = await self.db.get_business_by_name(name, interaction.guild_id)
        if existing:
            await interaction.response.send_message("A business with that name is already listed.", ephemeral=True)
            return

        owner_id = owner.id if owner else None
        biz_id = await self.db.create_business(interaction.guild_id, name, owner_id, initial_price, total_shares)

        founder_shares = 0
        if owner:
            pct = (founder_percent / 100) if founder_percent is not None else config.BUSINESS_FOUNDER_SHARE_PERCENT
            founder_shares = int(total_shares * pct)
            if founder_shares > 0:
                await self.db.adjust_available_shares(biz_id, -founder_shares)
                await self.db.upsert_holding(owner.id, interaction.guild_id, biz_id, founder_shares, 0)

        owner_text = owner.mention if owner else "publicly held (no owner)"
        founder_text = f" ({founder_shares:,} shares given to the owner as a founder stake)" if founder_shares else ""
        await interaction.response.send_message(
            f"📈 Listed **{name}** on the market at {fmt(int(initial_price))}/share "
            f"with {total_shares:,} total shares. Owner: {owner_text}{founder_text}. (ID: `{biz_id}`)"
        )

    # ------------------------------------------------------------------ #
    @business_group.command(name="adjust", description="[Admin] Move a business's stock price to reflect an IC event.")
    @app_commands.describe(
        business="Pick a listed business",
        change_percent="Percent change to apply, e.g. 15 for +15%, -20 for -20%",
        reason="Reason for the change (shown in stock history)",
    )
    @app_commands.autocomplete(business=listed_autocomplete)
    async def business_adjust(
        self,
        interaction: discord.Interaction,
        business: str,
        change_percent: app_commands.Range[float, -95.0, 500.0],
        reason: str,
    ):
        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        biz_id, name = biz[0], biz[1]
        # Same discipline as every other price writer: take the listing's lock,
        # re-read the price inside it, and write with the version we read.
        async with stockimpact.business_lock(biz_id):
            async with self.db.transaction() as tx:
                state = await self.db.get_business_trade_state(biz_id, tx=tx)
                price, version = state[3], state[8]
                new_price = await self.db.set_business_price(
                    biz_id, price * (1 + change_percent / 100), config.STOCK_MIN_PRICE,
                    expected_version=version, tx=tx,
                )
                if new_price is None:
                    raise RuntimeError(f"price_version conflict on business {biz_id}")
                await self.db.log_stock_event(
                    interaction.guild_id, biz_id, change_percent, reason, interaction.user.id, tx=tx
                )

        arrow = "📈" if change_percent >= 0 else "📉"
        await interaction.response.send_message(
            f"{arrow} **{name}** moved {change_percent:+.1f}% ({fmt(int(price))} → {fmt(int(new_price))}) — {reason}"
        )

    # ------------------------------------------------------------------ #
    @business_group.command(name="setowner", description="[Admin] Assign or clear a business's owner.")
    @app_commands.describe(business="Pick a listed business", owner="New owner (leave blank to make it publicly held)")
    @app_commands.autocomplete(business=listed_autocomplete)
    async def business_setowner(self, interaction: discord.Interaction, business: str, owner: discord.Member = None):
        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        await self.db.set_business_owner(biz[0], owner.id if owner else None)
        owner_text = owner.mention if owner else "publicly held (no owner)"
        await interaction.response.send_message(f"**{biz[1]}** is now {owner_text}.")

    # ------------------------------------------------------------------ #
    @business_group.command(
        name="link",
        description="[Admin] Tie a stock listing to a registered business so its revenue drives the price.",
    )
    @app_commands.describe(
        business="Pick a listed business (the stock)",
        company="The registered business whose revenue should drive it — leave blank to unlink",
    )
    @app_commands.autocomplete(business=listed_autocomplete, company=company_autocomplete)
    async def business_link(
        self, interaction: discord.Interaction, business: str, company: str = None
    ):
        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await send_error(interaction, "No listed business by that name.")
            return

        biz_id, biz_name, _owner, price, _prev, total, _available, _delisted = biz

        if company is None:
            # Back to automatic: match a registered business by name if one
            # exists. To switch revenue pricing off entirely, use `unlink`.
            await self.db.set_business_company_link(biz_id, None)
            fallback = await self.db.get_company_for_listing(biz_id, interaction.guild_id)
            note = (
                f"\nIt matches the registered business **{fallback[1]}** by name, so that business's "
                "revenue drives the price. Use `/eco-admin business unlink` to stop that completely."
                if fallback else
                "\nNo registered business shares its name, so its price drifts on the market alone."
            )
            await interaction.response.send_message(
                f"🔗 **{biz_name}** is back to matching by name.{note}"
            )
            return

        target = await self.db.get_company_by_name(company, interaction.guild_id)
        if not target:
            await send_error(interaction, "No registered business by that name — check `/company list`.")
            return

        company_id, company_name, _owner_id, _balance, revenue, _tax_paid, _created = target
        await self.db.set_business_company_link(biz_id, company_id)

        expected = stockimpact.expected_revenue(stockimpact.market_cap(price, total))
        await interaction.response.send_message(
            f"🔗 **{biz_name}** stock is now priced off **{company_name}**'s revenue "
            f"(all-time: {fmt(revenue)}).\n"
            f"At its current valuation it needs around **{fmt(int(expected))}** of revenue every "
            f"{config.STOCK_TICK_MINUTES} minutes to hold {fmt(int(price))}/share — more and the stock "
            "climbs, less and it slides."
        )

    # ------------------------------------------------------------------ #
    @business_group.command(
        name="unlink",
        description="[Admin] Remove a stock's link to a business, so revenue stops driving its price.",
    )
    @app_commands.describe(business="Pick a listed business (the stock)")
    @app_commands.autocomplete(business=listed_autocomplete)
    async def business_unlink(self, interaction: discord.Interaction, business: str):
        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await send_error(interaction, "No listed business by that name.")
            return

        biz_id, biz_name = biz[0], biz[1]
        previous = await self.db.get_company_for_listing(biz_id, interaction.guild_id)

        # NO_LINK is a deliberate "never link this one", unlike a plain clear:
        # it also stops the automatic name match from quietly re-linking it on
        # the very next tick.
        await self.db.set_business_company_link(biz_id, self.db.NO_LINK)

        was = f" It is no longer priced off **{previous[1]}**'s revenue." if previous else ""
        await interaction.response.send_message(
            f"🔓 **{biz_name}** has been unlinked.{was}\n"
            "Its price now moves only on trading and random drift. Re-link it any time with "
            "`/eco-admin business link`."
        )

    # ------------------------------------------------------------------ #
    @business_group.command(name="delist", description="[Admin] Delist a business, paying out all shareholders at the current price.")
    @app_commands.describe(business="Pick a listed business")
    @app_commands.autocomplete(business=listed_autocomplete)
    async def business_delist(self, interaction: discord.Interaction, business: str):
        biz = await self.db.get_business_by_name(business, interaction.guild_id)
        if not biz:
            await interaction.response.send_message("No listed business by that name.", ephemeral=True)
            return

        biz_id, name = biz[0], biz[1]
        # Under the listing's lock and in one transaction, so no /stock sell can
        # slip in between "read the holders" and "pay them out" and be paid
        # twice, and a crash mid-way can't leave half the holders paid.
        async with stockimpact.business_lock(biz_id):
            async with self.db.transaction() as tx:
                state = await self.db.get_business_trade_state(biz_id, tx=tx)
                price = state[3]
                holders = await self.db.get_all_holders(biz_id, tx=tx)
                for user_id, shares in holders:
                    payout = round(shares * price)
                    await self.db.add_cash(user_id, interaction.guild_id, payout, tx=tx)
                    await self.db.log_transaction(
                        user_id, interaction.guild_id, "stock_delist_payout", payout, f"{name} delisted", tx=tx
                    )
                    await self.db.clear_holding(user_id, interaction.guild_id, biz_id, tx=tx)
                await self.db.delist_business(biz_id, tx=tx)
        await interaction.response.send_message(
            f"**{name}** has been delisted at {fmt(int(price))}/share. {len(holders)} shareholder(s) were paid out."
        )

    # ------------------------------------------------------------------ #
    # Real company administration (ADMIN ONLY) — NOT the stock market
    # ------------------------------------------------------------------ #
    @company_group.command(name="create", description="[Admin] Register a real business (with a business account) for a player.")
    @app_commands.describe(
        name="Business name (must be unique)",
        owner="Player who owns this business",
    )
    async def company_create(self, interaction: discord.Interaction, name: str, owner: discord.Member):
        if len(name) > config.MAX_BUSINESS_NAME_LENGTH:
            await interaction.response.send_message(
                f"Business name must be {config.MAX_BUSINESS_NAME_LENGTH} characters or fewer.", ephemeral=True
            )
            return

        existing = await self.db.get_company_by_name(name.strip(), interaction.guild_id)
        if existing:
            await interaction.response.send_message("A business with that name already exists.", ephemeral=True)
            return

        company_id = await self.db.create_company(interaction.guild_id, name.strip(), owner.id)
        await interaction.response.send_message(
            f"🏢 Registered business **{name.strip()}** (ID: `{company_id}`) owned by {owner.mention}.\n"
            f"It has a business account starting at {fmt(0)}. All revenue is taxed at "
            f"{config.BUSINESS_TAX_RATE * 100:.0f}% — the owner pays via `/tax pay`."
        )

    # ------------------------------------------------------------------ #
    @company_group.command(
        name="delete",
        description="[Admin] Permanently delete a registered business (does NOT delist its stock).",
    )
    @app_commands.describe(
        business="Pick a registered business to delete",
        money="What happens to whatever is left in its business account",
    )
    @app_commands.autocomplete(business=company_autocomplete)
    @app_commands.choices(money=[
        app_commands.Choice(name="Settle the tax bill, refund the rest to the owner", value="owner"),
        app_commands.Choice(name="Send the whole balance to the treasury", value="treasury"),
        app_commands.Choice(name="Delete the money with the business", value="void"),
    ])
    async def company_delete(
        self,
        interaction: discord.Interaction,
        business: str,
        money: app_commands.Choice[str] = None,
    ):
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await send_error(interaction, "No business by that name.")
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        payout = money.value if money else "owner"
        owed = max(0, round(revenue * config.BUSINESS_TAX_RATE) - tax_paid)
        staff_count = await self.db.count_company_staff(company_id)
        ledger = await self.db.get_company_ledger(company_id, limit=1000)

        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        owner_name = owner_member.display_name if owner_member else (
            f"Unknown member ({owner_id})" if owner_id else "Ownerless"
        )

        if payout == "owner":
            tax_settled = min(balance, owed)
            refund = balance - tax_settled
            money_text = (
                f"{fmt(tax_settled)} goes to the treasury as outstanding tax and "
                f"{fmt(refund)} is refunded to **{owner_name}** in cash."
                if owner_id else
                f"{fmt(tax_settled)} goes to the treasury as outstanding tax. The business has no "
                f"owner to refund, so the remaining {fmt(refund)} goes to the treasury too."
            )
        elif payout == "treasury":
            money_text = f"The whole {fmt(balance)} balance goes to the server treasury."
        else:
            money_text = f"The {fmt(balance)} balance is destroyed along with the business."

        embed = discord.Embed(
            title=f"\u26a0\ufe0f Delete {name}?",
            description=(
                "This permanently removes the registered business, its staff roster and its entire "
                "ledger. It cannot be undone."
            ),
            color=discord.Color.red(),
        )
        embed.add_field(name="Owner", value=owner_name, inline=True)
        embed.add_field(name="Account Balance", value=fmt(balance), inline=True)
        embed.add_field(name="Outstanding Tax", value=fmt(owed), inline=True)
        embed.add_field(name="All-Time Revenue", value=fmt(revenue), inline=True)
        embed.add_field(name="Employees to remove", value=f"{staff_count}", inline=True)
        embed.add_field(name="Ledger entries to erase", value=f"{len(ledger)}", inline=True)
        embed.add_field(name="The money", value=money_text, inline=False)

        # The stock market is a separate system and is deliberately left alone.
        listing = await self.db.get_listing_for_company(company_id, interaction.guild_id)
        if listing:
            embed.add_field(
                name="\U0001f4c8 Its stock stays listed",
                value=(
                    f"**{listing[1]}** keeps trading at ${listing[2]:,.2f}/share and shareholders keep "
                    "their shares — it just stops being priced off this business's revenue.\n"
                    "To remove the stock as well (paying every shareholder out), use "
                    "`/eco-admin business delist` afterwards."
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed,
            view=CompanyDeleteView(self, interaction.user.id, company, payout),
            ephemeral=True,
        )

    async def perform_company_delete(self, interaction: discord.Interaction, company, payout: str):
        """Does the deletion once it has been confirmed, and returns the report."""
        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        gid = interaction.guild_id

        # Re-read: the confirmation window is 60 seconds, and money can move in
        # that time. Settling a stale balance would mint or destroy cash.
        current = await self.db.get_company_by_id(company_id, gid)
        if not current:
            return discord.Embed(
                title="Already gone",
                description=f"**{name}** no longer exists — nothing to delete.",
                color=discord.Color.greyple(),
            )
        company_id, name, owner_id, balance, revenue, tax_paid, created_at = current
        owed = max(0, round(revenue * config.BUSINESS_TAX_RATE) - tax_paid)

        tax_settled = refunded = to_treasury = 0
        if balance > 0:
            if payout == "owner" and owner_id:
                tax_settled = min(balance, owed)
                refunded = balance - tax_settled
                if tax_settled:
                    await self.db.add_treasury(gid, tax_settled)
                if refunded:
                    await self.db.add_cash(owner_id, gid, refunded)
                    await self.db.log_transaction(
                        owner_id, gid, "company_closure_payout", refunded,
                        f"{name} closed — account balance returned",
                    )
            else:
                # "treasury", "void" with no owner, or an ownerless business:
                # anything that isn't refunded goes to the treasury rather than
                # vanishing, unless the admin explicitly chose to void it.
                if payout == "void":
                    to_treasury = 0
                else:
                    to_treasury = balance
                    await self.db.add_treasury(gid, to_treasury)

        deleted = await self.db.delete_company(company_id, gid)

        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        owner_name = owner_member.display_name if owner_member else (
            f"Unknown member ({owner_id})" if owner_id else "Ownerless"
        )

        embed = discord.Embed(
            title=f"\U0001f5d1\ufe0f {name} has been deleted",
            description=f"Registered business closed by {interaction.user.mention}.",
            color=discord.Color.dark_red(),
        )
        embed.add_field(name="Former owner", value=owner_name, inline=True)
        embed.add_field(name="Employees removed", value=f"{deleted.get('company_staff', 0)}", inline=True)
        embed.add_field(name="Ledger entries erased", value=f"{deleted.get('company_ledger', 0)}", inline=True)

        money_lines = []
        if tax_settled:
            money_lines.append(f"{fmt(tax_settled)} settled the outstanding tax bill (to the treasury).")
        if refunded:
            money_lines.append(f"{fmt(refunded)} refunded to {owner_name} in cash.")
        if to_treasury:
            money_lines.append(f"{fmt(to_treasury)} transferred to the server treasury.")
        if payout == "void" and balance > 0:
            money_lines.append(f"{fmt(balance)} was destroyed with the business.")
        if not money_lines:
            money_lines.append("The business account was empty.")
        embed.add_field(name="The money", value="\n".join(money_lines), inline=False)

        embed.set_footer(text="The stock market was not touched — no stock was delisted.")
        return embed

    # ------------------------------------------------------------------ #
    @company_group.command(name="addrevenue", description="[Admin] Log money a business has received (e.g. RP income).")
    @app_commands.describe(
        business="Pick a registered business",
        amount="Amount the business received (e.g. 5000, 10k, 1.5m)",
        reason="What the money was for (shown in the ledger)",
    )
    @app_commands.autocomplete(business=company_autocomplete)
    async def company_addrevenue(
        self,
        interaction: discord.Interaction,
        business: str,
        amount: str,
        reason: str = "Revenue",
    ):
        amount, error = parse_amount(amount, noun="amount")
        if error:
            await send_error(interaction, error)
            return
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await interaction.response.send_message("No business by that name.", ephemeral=True)
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        tax_due = round((revenue + amount) * config.BUSINESS_TAX_RATE) - tax_paid

        # Revenue is what the stock market prices, so a listing tied to this
        # business reacts immediately (the market tick then prices the period
        # properly). Owner deposits never reach this path, so they never move a
        # stock. credit_revenue=True books the revenue and moves the price in
        # one locked transaction.
        impact = await stockimpact.apply_revenue_payment(
            self.db, interaction.guild_id, company_id, amount, reason, interaction.user.id,
            credit_revenue=True,
        )
        stock_line = "\n" + stockimpact.describe(*impact) if impact else ""

        await interaction.response.send_message(
            f"💰 **{name}** received {fmt(amount)} — {reason}.\n"
            f"All-time revenue: {fmt(revenue + amount)} | Outstanding tax: {fmt(max(0, tax_due))}"
            f"{stock_line}"
        )

    # ------------------------------------------------------------------ #
    @company_group.command(name="setowner", description="[Admin] Change who owns a registered business.")
    @app_commands.describe(
        business="Pick a registered business",
        owner="The player who should own it (leave blank to leave it ownerless)",
    )
    @app_commands.autocomplete(business=company_autocomplete)
    async def company_setowner(
        self, interaction: discord.Interaction, business: str, owner: discord.Member = None
    ):
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await send_error(interaction, "No business by that name.")
            return
        if owner and owner.bot:
            await send_error(interaction, "A bot can't own a business.")
            return

        company_id, name, old_owner_id, balance, revenue, tax_paid, created_at = company
        if owner:
            # The new owner can't also sit on the staff roster.
            await self.db.remove_company_staff(company_id, owner.id)
        await self.db.set_company_owner(company_id, owner.id if owner else None)

        previous = interaction.guild.get_member(old_owner_id) if old_owner_id else None
        previous_name = previous.display_name if previous else "nobody"
        new_name = owner.display_name if owner else "nobody (ownerless)"
        owed = max(0, round(revenue * config.BUSINESS_TAX_RATE) - tax_paid)
        note = f"\n⚠️ It still owes {fmt(owed)} in tax — that bill goes with the business." if owed else ""
        await interaction.response.send_message(
            f"📄 **{name}** has been transferred from **{previous_name}** to **{new_name}**, "
            f"along with its {fmt(balance)} account balance.{note}"
        )

    # ------------------------------------------------------------------ #
    @company_group.command(name="setstaff", description="[Admin] Hire, re-assign or remove a business's employee.")
    @app_commands.describe(
        business="Pick a registered business",
        user="The employee",
        position="Their job title — leave blank to remove them from the business",
    )
    @app_commands.autocomplete(business=company_autocomplete)
    @app_commands.choices(
        position=[app_commands.Choice(name=p, value=p) for p in config.COMPANY_POSITIONS[:25]]
    )
    async def company_setstaff(
        self,
        interaction: discord.Interaction,
        business: str,
        user: discord.Member,
        position: app_commands.Choice[str] = None,
    ):
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await send_error(interaction, "No business by that name.")
            return

        company_id, name, owner_id, *_ = company
        if position is None:
            if await self.db.remove_company_staff(company_id, user.id):
                await interaction.response.send_message(
                    f"👋 **{user.display_name}** no longer works at **{name}**."
                )
            else:
                await send_error(interaction, f"{user.display_name} doesn't work at **{name}**.")
            return

        if user.bot:
            await send_error(interaction, "Bots can't be employed.")
            return
        if user.id == owner_id:
            await send_error(
                interaction,
                f"{user.display_name} owns **{name}** — change the owner first with "
                "`/eco-admin company setowner`.",
            )
            return

        await self.db.set_company_staff(
            company_id, interaction.guild_id, user.id, position.value, interaction.user.id
        )
        await interaction.response.send_message(
            f"👔 **{user.display_name}** is now **{position.value}** at **{name}**."
        )

    # ------------------------------------------------------------------ #
    @company_group.command(name="info", description="[Admin] View a business's account, all-time revenue and tax status.")
    @app_commands.describe(business="Pick a registered business")
    @app_commands.autocomplete(business=company_autocomplete)
    async def company_info(self, interaction: discord.Interaction, business: str):
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await interaction.response.send_message("No business by that name.", ephemeral=True)
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        tax_due = max(0, round(revenue * config.BUSINESS_TAX_RATE) - tax_paid)

        owner_member = interaction.guild.get_member(owner_id) if owner_id else None
        owner_name = owner_member.display_name if owner_member else (f"Unknown ({owner_id})" if owner_id else "None")
        deposits = await self.db.get_company_deposits(company_id)

        embed = discord.Embed(title=f"🏢 {name}", color=discord.Color.dark_teal())
        embed.add_field(name="Owner", value=owner_name, inline=True)
        embed.add_field(name="Account Balance", value=fmt(balance), inline=True)
        embed.add_field(name="Registered", value=f"<t:{created_at}:R>", inline=True)
        embed.add_field(name="All-Time Revenue", value=fmt(revenue), inline=True)
        embed.add_field(name=f"Tax Paid ({config.BUSINESS_TAX_RATE * 100:.0f}%)", value=fmt(tax_paid), inline=True)
        embed.add_field(name="Outstanding Tax", value=fmt(tax_due), inline=True)
        # Owner deposits are personal money paid in, never revenue, so they are
        # shown separately and are not part of the tax base.
        embed.add_field(name="Owner Deposits (untaxed)", value=fmt(deposits), inline=True)

        staff = await self.db.get_company_staff(company_id)
        if staff:
            from cogs.companies import position_rank

            ordered = sorted(
                staff,
                key=lambda row: (position_rank(row[1]) if position_rank(row[1]) is not None else 99, row[2] or 0),
            )
            lines = []
            for user_id, position, _hired_at in ordered:
                member = interaction.guild.get_member(user_id)
                display = member.display_name if member else f"Unknown ({user_id})"
                lines.append(f"**{position}** · {display}")
            embed.add_field(name=f"Staff ({len(staff)})", value="\n".join(lines)[:1024], inline=False)

        ledger = await self.db.get_company_ledger(company_id, limit=5)
        if ledger:
            lines = [
                f"{'+' if amt >= 0 else ''}{fmt(amt)} — {desc} (<t:{ts}:R>)"
                for amt, desc, _, ts in ledger
            ]
            embed.add_field(name="Recent Ledger", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @company_group.command(name="ledger", description="[Admin] View every dollar a business has ever received or paid.")
    @app_commands.describe(business="Pick a registered business")
    @app_commands.autocomplete(business=company_autocomplete)
    async def company_ledger(self, interaction: discord.Interaction, business: str):
        company = await self.db.get_company_by_name(business, interaction.guild_id)
        if not company:
            await interaction.response.send_message("No business by that name.", ephemeral=True)
            return

        company_id, name, owner_id, balance, revenue, tax_paid, created_at = company
        entries = await self.db.get_company_ledger(company_id, limit=25)
        if not entries:
            await interaction.response.send_message(f"**{name}** has no ledger entries yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📒 {name} — Ledger (all-time revenue: {fmt(revenue)})",
            color=discord.Color.dark_teal(),
        )
        lines = [
            f"{'+' if amt >= 0 else ''}{fmt(amt)} — {desc} (<t:{ts}:R>)"
            for amt, desc, _, ts in entries
        ]
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    # FULL ECONOMY RESET — owner / co-owner only, irreversible
    # ------------------------------------------------------------------ #
    @admin_group.command(
        name="audit",
        description="[Admin] Reconcile the money supply against the transaction log and scan for abuse patterns.",
    )
    async def economy_audit(self, interaction: discord.Interaction):
        # Reading 1,500+ transactions is quick, but defer anyway so a large
        # server never trips Discord's 3-second reply deadline.
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, _flagged = await audit.run_reconciliation(
            self.bot, interaction.guild_id, trigger=f"{interaction.user.display_name} (/eco-admin audit)",
            post=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    @admin_group.command(
        name="reset-economy",
        description="[Owner only] Wipe the ENTIRE economy: balances, vehicles, loans, stocks and businesses.",
    )
    @app_commands.describe(
        confirm=f"Type exactly: {config.ECONOMY_RESET_CONFIRM_PHRASE}",
        also_reset_settings="Also clear the log / market / restricted channel settings (default: keep them)",
    )
    async def reset_economy(
        self,
        interaction: discord.Interaction,
        confirm: str,
        also_reset_settings: bool = False,
    ):
        if not may_reset_economy(interaction.user, interaction.guild):
            await send_error(
                interaction,
                "Only the server owner \u2014 or a co-owner listed in `ECONOMY_RESET_USER_IDS` in `config.py` "
                "\u2014 can reset the economy. An economy staff role or Administrator isn't enough, because "
                "this can't be undone.",
            )
            return

        phrase = config.ECONOMY_RESET_CONFIRM_PHRASE
        if confirm.strip() != phrase:
            await send_error(
                interaction,
                f"To reset the economy you have to type `{phrase}` (exactly, capitals included) into the "
                "`confirm` option.",
            )
            return

        counts = await self.db.count_guild_economy(interaction.guild_id)
        total = sum(counts.values())
        if total == 0:
            await interaction.response.send_message(
                "There's nothing to reset \u2014 this server has no economy data yet.", ephemeral=True
            )
            return

        lines = [
            f"• **{counts[table]:,}** — {RESET_TABLE_LABELS.get(table, table)}"
            for table in self.db.RESET_TABLES
            if counts.get(table)
        ]
        embed = discord.Embed(
            title="⚠️ Reset the entire economy?",
            description=(
                "This permanently deletes **everything** below for this server. There is no undo.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.red(),
        )
        embed.add_field(
            name="Afterwards",
            value=(
                "Every player starts again from the configured "
                f"{fmt(config.STARTING_CASH)} cash / {fmt(config.STARTING_BANK)} bank the first time they run "
                "a command."
                + (
                    "\nServer settings (log, market and restricted channels) are cleared too."
                    if also_reset_settings
                    else "\nServer settings (log, market and restricted channels) are kept."
                )
            ),
            inline=False,
        )
        backup_note = (
            "A database backup is uploaded to your backup channel first, so a mistake can still be undone."
            if getattr(config, "ECONOMY_RESET_BACKUP_FIRST", True)
            else "No backup will be taken first (ECONOMY_RESET_BACKUP_FIRST is off)."
        )
        embed.set_footer(text=f"{backup_note}\nThis confirmation expires in 60 seconds.")

        view = EconomyResetView(self, interaction.user.id, also_reset_settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def perform_reset(self, interaction: discord.Interaction, reset_settings: bool) -> discord.Embed:
        """Backs up, wipes, and returns a report embed. Called by the
        confirmation button above."""
        backup_line = "No backup was taken."
        if getattr(config, "ECONOMY_RESET_BACKUP_FIRST", True):
            backup_cog = self.bot.get_cog("Backup")
            if backup_cog:
                try:
                    backup_line = await backup_cog.send_backup(
                        f"before economy reset by {interaction.user.display_name}"
                    )
                except Exception as exc:  # a failed backup must not block the reset
                    log.exception("Pre-reset backup failed")
                    backup_line = f"Backup failed: {exc}"

        deleted = await self.db.reset_guild_economy(interaction.guild_id, reset_settings=reset_settings)
        total = sum(deleted.values())

        embed = discord.Embed(
            title="🧹 Economy reset",
            description=(
                f"Wiped **{total:,}** records. Every player starts from scratch with "
                f"{fmt(config.STARTING_CASH)} cash and {fmt(config.STARTING_BANK)} in the bank."
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        lines = [
            f"• **{count:,}** — {RESET_TABLE_LABELS.get(table, table)}"
            for table, count in deleted.items()
            if count
        ]
        if lines:
            embed.add_field(name="Deleted", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(name="Backup", value=backup_line, inline=False)
        # Plain display name, never a mention — the log should not ping anyone.
        embed.add_field(
            name="Reset by",
            value=f"{interaction.user.display_name} (`{interaction.user.id}`)",
            inline=False,
        )
        return embed

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            message = str(error) or "You don't have permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
