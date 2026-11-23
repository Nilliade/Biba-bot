"""Slash-команды игры «Угадай мелодию»."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from biba.bot import BibaBot
from biba.database import GuildSettings
from biba.game.controller import GameController
from biba.game.embeds import (
    error_embed,
    help_embed,
    history_embed,
    leaderboard_embed,
    profile_embed,
    settings_embed,
)

log = logging.getLogger(__name__)


class MelodyCog(commands.Cog):
    """Все команды группы ``/melody``."""

    melody = app_commands.Group(name="melody", description="Игра «Угадай мелодию»")

    def __init__(self, bot: BibaBot) -> None:
        self.bot = bot
        self.controllers: dict[int, GameController] = {}

    # -- помощники -------------------------------------------------------
    def _controller(self, guild_id: int) -> GameController | None:
        controller = self.controllers.get(guild_id)
        if controller is None:
            return None
        if controller.closed:
            # Игра давно закончилась — не держим объект в памяти.
            del self.controllers[guild_id]
            return None
        return controller

    def is_active(self, guild_id: int) -> bool:
        """Идёт ли сейчас игра — музыку на это время лучше не запускать."""
        return self._controller(guild_id) is not None

    async def shutdown(self) -> None:
        """Останавливает активные игры при выключении бота."""
        for controller in list(self.controllers.values()):
            try:
                await controller.cancel(None, "Бот перезапускается")
            except Exception:  # noqa: BLE001
                log.exception("Failed to cancel game in guild %s", controller.game.guild_id)
        self.controllers.clear()

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        embed = error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Failed to send error response")

    def _settings(self, interaction: discord.Interaction) -> GuildSettings:
        assert interaction.guild is not None
        return self.bot.settings.get(interaction.guild_id)

    # -- игра ------------------------------------------------------------
    @melody.command(name="start", description="Начать игру «Угадай мелодию»")
    @app_commands.describe(voice_channel="Голосовой канал (по умолчанию — ваш текущий)")
    async def start(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await self._error(interaction, "Играть можно только на сервере.")
            return
        if self._controller(interaction.guild_id) is not None:
            await self._error(interaction, "На этом сервере уже идёт игра.")
            return

        channel = voice_channel
        settings = self._settings(interaction)
        if channel is None:
            voice_state = getattr(interaction.user, "voice", None)
            channel = getattr(voice_state, "channel", None) if voice_state else None
        if channel is None and settings.voice_channel_id:
            channel = interaction.guild.get_channel(settings.voice_channel_id)
        if channel is not None and not isinstance(
            channel, (discord.VoiceChannel, discord.StageChannel)
        ):
            channel = None
        if channel is None:
            await self._error(
                interaction,
                "Зайдите в голосовой канал или укажите его в команде `/melody start`.",
            )
            return

        me = interaction.guild.me
        if me is None:
            await self._error(interaction, "Не удалось получить данные бота на сервере.")
            return
        permissions = channel.permissions_for(me)
        if not permissions.connect or not permissions.speak:
            await self._error(
                interaction,
                "У бота нет прав «Подключаться» и «Говорить» в этом канале.",
            )
            return

        if self.bot.playlists.count(interaction.guild_id, settings.playlist_key) == 0:
            await self._error(
                interaction,
                "Плейлист пуст. Добавьте хотя бы один трек командой `/playlist add`.",
            )
            return

        try:
            game = self.bot.engine.create_game(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                voice_channel_id=channel.id,
                host_id=interaction.user.id,
                rounds=settings.rounds,
                snippet_sec=settings.snippet_sec,
                betting_seconds=settings.betting_seconds,
                host_fee_percent=settings.host_fee_percent,
                min_bet=settings.min_bet,
                starting_balance=settings.starting_balance,
                host_role_id=settings.host_role_id,
                playlist_key=settings.playlist_key,
                random_offset=settings.random_offset,
            )
        except Exception as exc:  # noqa: BLE001
            await self._error(interaction, str(exc))
            return

        controller = GameController(
            bot=self.bot,
            engine=self.bot.engine,
            resolver=self.bot.resolver,
            voice=self.bot.voice,
            history=self.bot.history,
            guild=interaction.guild,
            game=game,
            settings=settings,
        )
        self.controllers[interaction.guild_id] = controller
        # Игра занимает голосовой канал: музыку ставим на паузу.
        await self.bot.music.pause_for_game(interaction.guild_id)
        await controller.send_lobby(interaction)
        if controller.message is None:
            self.bot.engine.cancel(interaction.guild_id, "Не удалось создать сообщение игры")
            self.bot.engine.remove_game(interaction.guild_id)
            self.controllers.pop(interaction.guild_id, None)
            await self.bot.music.resume_after_game(interaction.guild_id)
            await self._error(interaction, "Не удалось создать сообщение игры.")

    @melody.command(name="join", description="Присоединиться к игре")
    async def join(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._error(interaction, "Играть можно только на сервере.")
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.join(interaction)

    @melody.command(name="leave", description="Покинуть игру")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Вы не участвуете в игре.")
            return
        await controller.leave(interaction)

    @melody.command(name="bet", description="Сделать ставку")
    @app_commands.describe(amount="Сумма ставки в баллах")
    async def bet(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.handle_bet(interaction, amount)

    @melody.command(name="guess", description="Угадать мелодию текстом")
    @app_commands.describe(title="Название или исполнитель")
    async def guess(self, interaction: discord.Interaction, title: str) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.handle_guess(interaction, title[:100])

    @melody.command(name="host", description="Открыть панель ведущего")
    async def host(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.reopen_host_panel(interaction)

    @melody.command(name="next", description="Запустить следующий раунд (для ведущего)")
    async def next(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.next_round(interaction)

    @melody.command(name="stop", description="Остановить игру (ведущий или администратор)")
    async def stop(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        controller = self._controller(interaction.guild_id)
        if controller is None:
            await self._error(interaction, "Сейчас нет активной игры.")
            return
        await controller.cancel_from_interaction(interaction)

    @melody.command(name="top", description="Таблица лидеров по баллам")
    @app_commands.describe(limit="Сколько игроков показать")
    async def top(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 3, 25] = 10,
    ) -> None:
        if interaction.guild is None:
            return
        rows = self.bot.wallet.leaderboard(interaction.guild_id, limit)
        await interaction.response.send_message(
            embed=leaderboard_embed(rows, interaction.guild), ephemeral=False
        )

    @melody.command(name="profile", description="Статистика игрока")
    @app_commands.describe(user="Игрок (по умолчанию — вы)")
    async def profile(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        target = user or interaction.user
        stats = self.bot.wallet.stats(interaction.guild_id, target.id)
        settings = self.bot.settings.get(interaction.guild_id)
        await interaction.response.send_message(
            embed=profile_embed(stats, target, starting_balance=settings.starting_balance),
            ephemeral=False,
        )

    @melody.command(name="history", description="Последние раунды на сервере")
    @app_commands.describe(limit="Сколько раундов показать")
    async def history(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if interaction.guild is None:
            return
        rows = self.bot.history.recent(interaction.guild_id, limit)
        await interaction.response.send_message(
            embed=history_embed(rows, interaction.guild), ephemeral=True
        )

    @melody.command(name="settings", description="Настроить игру (только администраторы)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        rounds="Количество раундов (1–20)",
        snippet="Длительность превью: 5, 10, 15, 20 или 30 секунд",
        betting_seconds="Время на ставки (10–120 секунд)",
        starting_balance="Стартовый баланс (100–100 000)",
        host_fee="Процент ведущего от банка (0–50)",
        min_bet="Минимальная ставка (1–10 000)",
        random_offset="Запускать превью с случайного места",
        host_role="Discord-роль, которая переходит к ведущему",
        text_channel="Канал для сообщений игры",
        voice_channel="Голосовой канал по умолчанию",
        playlist="Ключ плейлиста с треками",
    )
    async def settings(
        self,
        interaction: discord.Interaction,
        rounds: app_commands.Range[int, 1, 20] | None = None,
        snippet: app_commands.Range[int, 5, 30] | None = None,
        betting_seconds: app_commands.Range[int, 10, 120] | None = None,
        starting_balance: app_commands.Range[int, 100, 100_000] | None = None,
        host_fee: app_commands.Range[int, 0, 50] | None = None,
        min_bet: app_commands.Range[int, 1, 10_000] | None = None,
        random_offset: bool | None = None,
        host_role: discord.Role | None = None,
        text_channel: discord.TextChannel | None = None,
        voice_channel: discord.VoiceChannel | None = None,
        playlist: str | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        changes: dict[str, Any] = {}
        if rounds is not None:
            changes["rounds"] = rounds
        if snippet is not None:
            changes["snippet_sec"] = snippet
        if betting_seconds is not None:
            changes["betting_seconds"] = betting_seconds
        if starting_balance is not None:
            changes["starting_balance"] = starting_balance
        if host_fee is not None:
            changes["host_fee_percent"] = host_fee
        if min_bet is not None:
            changes["min_bet"] = min_bet
        if random_offset is not None:
            changes["random_offset"] = random_offset
        if host_role is not None:
            changes["host_role_id"] = host_role.id
        if text_channel is not None:
            changes["text_channel_id"] = text_channel.id
        if voice_channel is not None:
            changes["voice_channel_id"] = voice_channel.id
        if playlist is not None:
            changes["playlist_key"] = playlist.strip().lower().replace(" ", "_")

        try:
            current = self.bot.settings.update(interaction.guild_id, **changes)
        except ValueError as exc:
            await self._error(interaction, str(exc))
            return
        await interaction.response.send_message(embed=settings_embed(current), ephemeral=True)

    @settings.autocomplete("playlist")
    async def playlist_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        assert interaction.guild is not None
        names = self.bot.playlists.names(interaction.guild_id)
        return [
            app_commands.Choice(name=name, value=name)
            for name in names
            if current.lower() in name.lower()
        ][:25]

    @melody.command(name="help", description="Правила игры и команды")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)

    # -- обработка ошибок группы ----------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await self._error(interaction, "Нужны права администратора для этой команды.")
            return
        log.error("Melody command error", exc_info=error)
        await self._error(interaction, "Не удалось выполнить команду.")


async def setup(bot: BibaBot) -> None:
    await bot.add_cog(MelodyCog(bot))
