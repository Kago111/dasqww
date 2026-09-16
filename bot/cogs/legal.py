import discord
from discord import app_commands
from discord.ext import commands

import config
from amounts import parse_amount
from cogs.economy import fmt, send_error


def has_police_role():
    """Gate for police commands, matched by role ID (config.POLICE_ROLE_IDS).

    Matching by role NAME meant anyone who could create a role — or who held
    any role that happened to be called "Police" — could issue $100,000 fines
    and set wanted levels. Role IDs can't be renamed or spoofed."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if interaction.user.guild_permissions.administrator:
            return True
        if {r.id for r in interaction.user.roles}.intersection(config.POLICE_ROLE_IDS):
            return True
        raise app_commands.CheckFailure("You need a Police role to use this command.")
    return app_commands.check(predicate)


class Legal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------------ #
    @app_commands.command(name="fine", description="[Police] Issue a fine to a player.")
    @app_commands.describe(user="Who to fine", amount="Fine amount (e.g. 500, 10k)", reason="Reason for the fine")
    @has_police_role()
    async def fine(self, interaction: discord.Interaction, user: discord.Member, amount: str, reason: str):
        amount, error = parse_amount(amount, minimum=1, maximum=100_000, noun="fine")
        if error:
            await send_error(interaction, error)
            return
        fine_id = await self.db.add_fine(user.id, interaction.guild_id, amount, reason, interaction.user.id)
        await self.db.adjust_credit_score(
            user.id, interaction.guild_id, -config.CREDIT_SCORE_FINE_PENALTY,
            config.MIN_CREDIT_SCORE, config.MAX_CREDIT_SCORE,
        )
        await interaction.response.send_message(
            f"🚨 {user.mention} has been fined **{fmt(amount)}** for: *{reason}*\n"
            f"They can pay with `/payfine {fine_id}`. (Credit score -{config.CREDIT_SCORE_FINE_PENALTY})"
        )

    # ------------------------------------------------------------------ #
    @app_commands.command(name="fines", description="View your (or someone else's) unpaid fines.")
    @app_commands.describe(user="Whose fines to view (defaults to you)")
    async def fines(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        rows = await self.db.get_unpaid_fines(target.id, interaction.guild_id)
        if not rows:
            await interaction.response.send_message(f"{target.display_name} has no unpaid fines.")
            return

        lines = [f"`#{fid}` {fmt(amount)} — {reason}" for fid, amount, reason, issued_by in rows]
        embed = discord.Embed(
            title=f"{target.display_name}'s Unpaid Fines",
            description="\n".join(lines),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="payfine", description="Pay off one of your fines.")
    @app_commands.describe(fine_id="The fine ID (see /fines)")
    async def payfine(self, interaction: discord.Interaction, fine_id: int):
        uid, gid = interaction.user.id, interaction.guild_id
        fine_row = await self.db.get_fine(fine_id, gid)
        if not fine_row or fine_row[1] != uid:
            await interaction.response.send_message("That fine doesn't belong to you.", ephemeral=True)
            return

        _, _, amount, paid = fine_row
        if paid:
            await send_error(interaction, "That fine is already paid.")
            return

        if not await self.db.try_spend_cash(uid, gid, amount):
            cash, _ = await self.db.get_balance(uid, gid)
            await send_error(interaction, f"You need {fmt(amount)} in cash to pay this fine. You have {fmt(cash)}.")
            return

        await self.db.add_treasury(gid, amount)
        await self.db.mark_fine_paid(fine_id)
        await self.db.log_transaction(uid, gid, "fine_paid", -amount, f"Fine #{fine_id}")
        await interaction.response.send_message(f"Fine #{fine_id} paid — **{fmt(amount)}** deducted.")

    # ------------------------------------------------------------------ #
    @app_commands.command(name="wanted", description="[Police] Set a player's wanted level (0-5).")
    @app_commands.describe(user="Target player", level="Wanted level from 0 (clear) to 5 (max)")
    @has_police_role()
    async def wanted(self, interaction: discord.Interaction, user: discord.Member, level: app_commands.Range[int, 0, 5]):
        await self.db.set_wanted(user.id, interaction.guild_id, level)
        if level == 0:
            await interaction.response.send_message(f"{user.mention}'s wanted level has been cleared.")
        else:
            stars = "⭐" * level
            await interaction.response.send_message(f"{user.mention} is now wanted: {stars} ({level}/5)")

    @app_commands.command(name="wantedstatus", description="Check a player's current wanted level.")
    @app_commands.describe(user="Whose wanted level to check (defaults to you)")
    async def wantedstatus(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        level = await self.db.get_wanted(target.id, interaction.guild_id)
        stars = "⭐" * level if level else "Clean record"
        await interaction.response.send_message(f"{target.display_name}: {stars}")

    # ------------------------------------------------------------------ #
    @app_commands.command(name="bounty", description="Post a cash bounty on a player's head (funds go to server treasury for payout by staff).")
    @app_commands.describe(user="Target player", amount="Bounty amount to fund from your cash (e.g. 5000, 10k)")
    async def bounty(self, interaction: discord.Interaction, user: discord.Member, amount: str):
        uid, gid = interaction.user.id, interaction.guild_id
        if user.id == uid:
            await send_error(interaction, "You can't post a bounty on yourself.")
            return

        cash, _ = await self.db.get_balance(uid, gid)
        amount, error = parse_amount(amount, minimum=1, maximum=1_000_000, available=cash, noun="bounty")
        if error:
            await send_error(interaction, error)
            return
        if not await self.db.try_spend_cash(uid, gid, amount):
            await send_error(interaction, f"You only have {fmt(cash)} in cash.")
            return

        await self.db.add_treasury(gid, amount)
        await self.db.log_transaction(uid, gid, "bounty_posted", -amount, f"Bounty on {user.display_name}")
        await interaction.response.send_message(
            f"💀 {interaction.user.mention} posted a **{fmt(amount)}** bounty on {user.mention}!\n"
            f"(Funds held in server treasury — staff should coordinate payout when the bounty is claimed IC.)"
        )

    # ------------------------------------------------------------------ #
    @app_commands.command(name="arrest", description="[Police] Mark a player as arrested, clearing their wanted level.")
    @app_commands.describe(user="Player being arrested")
    @has_police_role()
    async def arrest(self, interaction: discord.Interaction, user: discord.Member):
        await self.db.set_wanted(user.id, interaction.guild_id, 0)
        await interaction.response.send_message(f"🚔 {user.mention} has been arrested by {interaction.user.mention}. Wanted level cleared.")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                await interaction.followup.send(str(error), ephemeral=True)
            else:
                await interaction.response.send_message(str(error), ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Legal(bot))
