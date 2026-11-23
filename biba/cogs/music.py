"""Музыкальные команды: воспроизведение, очередь, громкость, 24/7."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from biba.bot import BibaBot
from biba.game.embeds import error_embed
from biba.music.embeds import (
    music_settings_embed,
    now_playing_embed,
    play_result_embed,
    queue_embed,
    stopped_embed,
    volume_embed,
)
from biba.music.resolver import ResolveError
from biba.music.service import MusicError

log = logging.getLogger(__name__)


class MusicCog(commands.Cog):
    """Группа ``/music`` — привычный музыкальный бот."""

    music = app_commands.Group(name="music", description="Музыка: очередь, громкость, 24/7")

    def __init__(self, bot: BibaBot) -> None:
        self.bot = bot

    # -- помощники -------------------------------------------------------
    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        embed = error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Failed to send music error response")

    async def _send(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Failed to send music response")

    def _game_active(self, guild_id: int) -> bool:
        melody = self.bot.get_cog("MelodyCog")
        is_active = getattr(melody, "is_active", None)
        if is_active is None:
            return False
        return bool(is_active(guild_id))

    def _voice_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        state = getattr(interaction.user, "voice", None)
        channel = getattr(state, "channel", None) if state is not None else None
        if channel is None:
            settings = self.bot.music_settings.get(interaction.guild_id)
            if settings.voice_channel_id:
                channel = interaction.guild.get_channel(settings.voice_channel_id)
        if channel is not None and not isinstance(
            channel, (discord.VoiceChannel, discord.StageChannel)
        ):
            return None
        return channel  # type: ignore[return-value]

    async def _permissions_ok(
        self, interaction: discord.Interaction, channel: discord.abc.Connectable | None
    ) -> bool:
        me = interaction.guild.me if interaction.guild is not None else None
        if me is None or channel is None:
            await self._error(interaction, "Не удалось проверить права в канале.")
            return False
        perms = channel.permissions_for(me)
        if not perms.connect or not perms.speak:
            await self._error(
                interaction,
                "У бота нет прав «Подключаться» и «Говорить» в этом канале.",
            )
            return False
        return True

    async def _dj_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        settings = self.bot.music_settings.get(interaction.guild_id)
        if not settings.dj_role_id:
            return True
        if getattr(getattr(interaction.user, "guild_permissions", None), "manage_guild", False):
            return True
        role = interaction.guild.get_role(settings.dj_role_id)
        if role is None:
            return True
        if role in getattr(interaction.user, "roles", []):
            return True
        await self._error(
            interaction,
            f"Управлять музыкой может только {role.mention} или администратор.",
        )
        return False

    # -- воспроизведение --------------------------------------------------
    @music.command(name="play", description="Включить трек или добавить в очередь")
    @app_commands.describe(query="Название, исполнитель или ссылка")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if interaction.guild is None:
            return
        if self._game_active(interaction.guild_id):
            await self._error(
                interaction, "Идёт игра «Угадай мелодию» — музыка вернётся после раунда."
            )
            return
        if not await self._dj_allowed(interaction):
            return
        channel = self._voice_channel(interaction)
        if channel is None:
            await self._error(
                interaction, "Зайдите в голосовой канал или укажите его в /music 247."
            )
            return
        if not await self._permissions_ok(interaction, channel):
            return
        await interaction.response.defer()
        try:
            result = await self.bot.music.play_query(
                interaction.guild,
                channel,
                query,
                interaction.user.id,
                text_channel_id=interaction.channel_id,
            )
        except ResolveError as exc:
            await self._error(interaction, str(exc))
            return
        except MusicError as exc:
            await self._error(interaction, str(exc))
            return
        await self._send(interaction, play_result_embed(result))

    @music.command(name="pause", description="Поставить на паузу")
    async def pause(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        if not self.bot.music.pause(interaction.guild):
            await self._error(interaction, "Сейчас ничего не играет.")
            return
        await interaction.response.send_message("⏸️ Пауза.", ephemeral=True)

    @music.command(name="resume", description="Продолжить воспроизведение")
    async def resume(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        if not self.bot.music.resume(interaction.guild):
            await self._error(interaction, "Нет поставленной на паузу дорожки.")
            return
        await interaction.response.send_message("▶️ Продолжаю.", ephemeral=True)

    @music.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        if not await self.bot.music.skip(interaction.guild):
            await self._error(interaction, "Очередь пуста.")
            return
        await interaction.response.send_message("⏭️ Пропускаю.", ephemeral=True)

    @music.command(name="stop", description="Остановить музыку и очистить очередь")
    async def stop(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        count = await self.bot.music.stop(interaction.guild)
        await interaction.response.send_message(embed=stopped_embed(count), ephemeral=True)

    @music.command(name="join", description="Подключить бота в голосовой канал")
    @app_commands.describe(channel="Голосовой канал (по умолчанию — ваш текущий)")
    async def join(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        target = channel or self._voice_channel(interaction)
        if target is None:
            await self._error(interaction, "Укажите голосовой канал.")
            return
        if not await self._permissions_ok(interaction, target):
            return
        if not await self.bot.music.connect(interaction.guild, target):
            await self._error(interaction, "Не удалось подключиться к каналу.")
            return
        await interaction.response.send_message(
            f"🎧 Подключился к {target.mention}.", ephemeral=True
        )

    @music.command(name="leave", description="Отключить бота от голосового канала")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        count = await self.bot.music.stop(interaction.guild)
        await interaction.response.send_message(
            f"👋 Вышел из канала. Удалено из очереди: {count}.", ephemeral=True
        )

    @music.command(name="queue", description="Показать очередь")
    @app_commands.describe(limit="Сколько треков показать")
    async def queue(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if interaction.guild is None:
            return
        player = self.bot.music.player(interaction.guild_id)
        await interaction.response.send_message(
            embed=queue_embed(player, interaction.guild, limit), ephemeral=True
        )

    @music.command(name="nowplaying", description="Что играет прямо сейчас")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        player = self.bot.music.player(interaction.guild_id)
        if player.current is None:
            await self._error(interaction, "Сейчас ничего не играет.")
            return
        await interaction.response.send_message(
            embed=now_playing_embed(player.current), ephemeral=True
        )

    @music.command(name="volume", description="Громкость (0–200%)")
    @app_commands.describe(value="Процент громкости")
    async def volume(
        self,
        interaction: discord.Interaction,
        value: app_commands.Range[int, 0, 200],
    ) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        result = await self.bot.music.set_volume(interaction.guild, value)
        await interaction.response.send_message(embed=volume_embed(result), ephemeral=True)

    @music.command(name="shuffle", description="Перемешать очередь")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        enabled = self.bot.music.toggle_shuffle(interaction.guild_id)
        await interaction.response.send_message(
            "🔀 Перемешивание включено." if enabled else "🔀 Перемешивание выключено.",
            ephemeral=True,
        )

    @music.command(name="loop", description="Повтор: выключено / трек / очередь")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Выключено", value="off"),
            app_commands.Choice(name="Трек", value="track"),
            app_commands.Choice(name="Очередь", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: str) -> None:
        if interaction.guild is None:
            return
        if not await self._dj_allowed(interaction):
            return
        try:
            result = self.bot.music.set_loop(interaction.guild_id, mode)
        except MusicError as exc:
            await self._error(interaction, str(exc))
            return
        labels = {"off": "выключен", "track": "повтор трека", "queue": "повтор очереди"}
        await interaction.response.send_message(
            f"🔁 Повтор: {labels.get(result, result)}.", ephemeral=True
        )

    @music.command(name="247", description="Режим 24/7 — бот остаётся в канале")
    @app_commands.describe(
        enabled="Включить или выключить",
        channel="Голосовой канал (по умолчанию — текущий)",
    )
    async def stay(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        target = channel or self._voice_channel(interaction)
        if enabled and target is None:
            await self._error(interaction, "Укажите голосовой канал.")
            return
        if target is not None and not await self._permissions_ok(interaction, target):
            return
        settings = self.bot.music_settings.update(
            interaction.guild_id,
            twenty_four_seven=enabled,
            voice_channel_id=target.id if target else None,
        )
        if enabled and target is not None:
            if not await self.bot.music.connect(interaction.guild, target):
                await self._error(interaction, "Не удалось подключиться к каналу.")
                return
        await interaction.response.send_message(
            embed=music_settings_embed(settings, guild=interaction.guild), ephemeral=True
        )

    @music.command(name="djrole", description="Роль, которая может управлять музыкой")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Роль DJ (пусто — сбросить)")
    async def djrole(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        settings = self.bot.music_settings.update(
            interaction.guild_id, dj_role_id=role.id if role is not None else None
        )
        await interaction.response.send_message(
            embed=music_settings_embed(settings, guild=interaction.guild), ephemeral=True
        )

    @music.command(name="settings", description="Настройки музыки на сервере")
    async def settings(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        current = self.bot.music_settings.get(interaction.guild_id)
        await interaction.response.send_message(
            embed=music_settings_embed(current, guild=interaction.guild), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await self.bot.music.on_voice_state_update(member, before, after)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await self._error(interaction, "Нужны права администратора для этой команды.")
            return
        log.error("Music command error", exc_info=error)
        await self._error(interaction, "Не удалось выполнить команду.")


async def setup(bot: BibaBot) -> None:
    await bot.add_cog(MusicCog(bot))
