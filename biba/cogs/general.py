"""Общие команды: /ping, /help, /about."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import biba
from biba.bot import BibaBot
from biba.game.embeds import help_embed


class GeneralCog(commands.Cog):
    def __init__(self, bot: BibaBot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Задержка бота")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = self.bot.latency * 1000
        await interaction.response.send_message(f"🏓 Понг! `{latency:.0f} мс`", ephemeral=True)

    @app_commands.command(name="help", description="Правила игры и список команд")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)

    @app_commands.command(name="about", description="О боте")
    async def about(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎵 Biba — Угадай мелодию",
            description=(
                "Музыкальный Discord-бот с игрой «Угадай мелодию»: ставки баллами, "
                "множители за скорость, ротация ведущего и постоянная таблица лидеров."
            ),
            color=0x8B5CF6,
        )
        embed.add_field(name="Версия", value=biba.__version__, inline=True)
        embed.add_field(name="Серверов", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Задержка", value=f"{self.bot.latency * 1000:.0f} мс", inline=True)
        embed.set_footer(text="Смотрите /help для правил игры.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: BibaBot) -> None:
    await bot.add_cog(GeneralCog(bot))
