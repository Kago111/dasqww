from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.economy import fmt, send_error


def detect_job_from_roles(member: discord.Member) -> Optional[str]:
    """Returns the config.JOBS key matching one of the member's Discord roles.

    Jobs are Police/EMS/DOT only and can no longer be self-applied — this is
    the only way a player gets a job. Matching is by role ID
    (config.JOB_ROLE_IDS), since IDs can't be renamed or confused with
    another role and several roles can map to the same job. Falls back to a
    case-insensitive name match against the job name for servers that
    haven't filled in JOB_ROLE_IDS yet."""
    member_role_ids = {role.id for role in member.roles}
    for job, role_ids in config.JOB_ROLE_IDS.items():
        if member_role_ids.intersection(role_ids):
            return job

    role_names = {role.name.lower() for role in member.roles}
    for job in config.JOBS:
        if job.lower() in role_names:
            return job
    return None


class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def _resolve_job(self, member: discord.Member, guild_id: int):
        """Returns the member's job. If they have no stored job but hold a
        matching Discord role, the job is assigned automatically."""
        job, _clocked_in_at = await self.db.get_job(member.id, guild_id)
        if not job:
            detected = detect_job_from_roles(member)
            if detected:
                await self.db.set_job(member.id, guild_id, detected)
                job = detected
        return job

    # ------------------------------------------------------------------ #
    @app_commands.command(name="joblist", description="List all available jobs on the server.")
    async def joblist(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Available Jobs", color=discord.Color.blue())
        for name, data in config.JOBS.items():
            embed.add_field(
                name=f"{name} — {fmt(data['hourly_pay'])}/hr",
                value=data["description"],
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ #
    @app_commands.command(name="jobinfo", description="Check your current job and shift status.")
    async def jobinfo(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        job = await self._resolve_job(interaction.user, gid)
        if not job:
            job_list = ", ".join(config.JOBS.keys())
            await send_error(
                interaction,
                f"You aren't employed. Jobs are only available to members holding a {job_list} role on this server "
                "— ask staff to assign one.",
            )
            return

        hourly = config.JOBS.get(job, {}).get("hourly_pay", 0)

        embed = discord.Embed(title=f"{interaction.user.display_name}'s Job", color=discord.Color.blue())
        embed.add_field(name="Job", value=job)
        embed.add_field(name="Pay Rate", value=f"{fmt(hourly)}/hr")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Jobs(bot))
