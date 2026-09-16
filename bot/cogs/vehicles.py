import random
import string

import discord
from discord import app_commands
from discord.ext import commands

from cogs.economy import fmt, send_error


def generate_plate() -> str:
    letters = "".join(random.choices(string.ascii_uppercase, k=3))
    digits = "".join(random.choices(string.digits, k=3))
    return f"{letters}-{digits}"


class Vehicles(commands.Cog):
    """Garage listing. Vehicles are registered to a player's garage by staff
    via /eco-admin givevehicle."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def _owned_vehicle(self, interaction: discord.Interaction, vehicle_id: int):
        """Returns the vehicle row if it exists and belongs to the caller, else replies with an error and returns None."""
        vehicle = await self.db.get_vehicle(vehicle_id, interaction.guild_id)
        if not vehicle or vehicle[1] != interaction.user.id:
            await send_error(interaction, "You don't own a vehicle with that ID. Check `/garage`.")
            return None
        return vehicle

    # ------------------------------------------------------------------ #
    @app_commands.command(name="garage", description="View your (or someone else's) owned vehicles.")
    @app_commands.describe(user="Whose garage to view (defaults to you)")
    async def garage(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        rows = await self.db.get_garage(target.id, interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                f"{target.display_name} doesn't own any vehicles. Staff can register one with `/eco-admin givevehicle`."
            )
            return

        embed = discord.Embed(title=f"\U0001f697 {target.display_name}'s Garage", color=discord.Color.dark_teal())
        total_value = 0
        for vid, name, category, price, condition, _insured, plate in rows:
            current_value = int(price * condition / 100)
            total_value += current_value
            embed.add_field(
                name=f"{name} ({plate})",
                value=(
                    f"Category: {category}\n"
                    f"Condition: {condition}%\n"
                    f"Value: {fmt(current_value)}\n"
                    f"ID: `{vid}`"
                ),
                inline=True,
            )
        embed.set_footer(text=f"{len(rows)} vehicle(s) \u00b7 Total value {fmt(total_value)}")
        await interaction.response.send_message(embed=embed)

    # Vehicle insurance (/insure and /claim) has been removed entirely.
    # The old claim payout was calculated from a vehicle's full purchase price
    # for a flat $25 premium, which let any player mint unlimited cash.


async def setup(bot: commands.Bot):
    await bot.add_cog(Vehicles(bot))
