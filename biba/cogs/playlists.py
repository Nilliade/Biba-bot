"""Slash-команды для управления плейлистами."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from biba.bot import BibaBot
from biba.game.embeds import error_embed, playlist_embed, playlists_embed, track_embed
from biba.music.resolver import ResolveError

log = logging.getLogger(__name__)


def playlist_key(name: str | None) -> tuple[str, str]:
    """Возвращает (ключ, отображаемое имя) для плейлиста."""
    raw = (name or "").strip()
    if not raw:
        return "melody", "Угадай мелодию"
    key = raw.lower().replace(" ", "_")
    return key, raw


class PlaylistCog(commands.Cog):
    """Команды группы ``/playlist``."""

    playlist = app_commands.Group(name="playlist", description="Плейлисты для игры")

    def __init__(self, bot: BibaBot) -> None:
        self.bot = bot

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        embed = error_embed(message)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @playlist.command(name="add", description="Добавить трек в плейлист")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        query="Название, исполнитель или ссылка на трек",
        name="Имя плейлиста (по умолчанию — melody)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        query: str,
        name: str | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        key, display_name = playlist_key(name)
        await interaction.response.defer()
        try:
            track = await self.bot.resolver.resolve(query)
        except ResolveError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        added = self.bot.playlists.add_track(
            interaction.guild_id,
            key,
            display_name,
            track,
            added_by=str(interaction.user.id),
        )
        if not added:
            await interaction.followup.send(
                embed=error_embed("Этот трек уже есть в плейлисте."), ephemeral=True
            )
            return
        await interaction.followup.send(embed=track_embed(track, key=key))

    @playlist.command(name="list", description="Список плейлистов сервера")
    async def list_playlists(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        rows = self.bot.playlists.list(interaction.guild_id)
        await interaction.response.send_message(embed=playlists_embed(rows), ephemeral=True)

    @playlist.command(name="show", description="Показать треки плейлиста")
    @app_commands.describe(name="Имя плейлиста (по умолчанию — melody)")
    async def show(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        key, display_name = playlist_key(name)
        info = self.bot.playlists.get(interaction.guild_id, key)
        if info is not None:
            display_name = info["name"]
        tracks = self.bot.playlists.tracks(interaction.guild_id, key)
        await interaction.response.send_message(
            embed=playlist_embed(
                key,
                display_name,
                tracks,
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )

    @playlist.command(name="remove", description="Удалить трек из плейлиста")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        track_id="ID трека из /playlist show",
        name="Имя плейлиста (по умолчанию — melody)",
    )
    async def remove(
        self,
        interaction: discord.Interaction,
        track_id: str,
        name: str | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        key, _ = playlist_key(name)
        track = self.bot.playlists.remove_track(interaction.guild_id, key, track_id)
        if track is None:
            await self._error(interaction, "Трек с таким ID не найден.")
            return
        await interaction.response.send_message(
            f"🗑️ Удалён из плейлиста: **{track.label}**", ephemeral=True
        )

    @remove.autocomplete("track_id")
    async def track_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        options = {
            option.get("name"): option.get("value")
            for option in (interaction.data or {}).get("options", [])
            if isinstance(option, dict)
        }
        key, _ = playlist_key(options.get("name"))
        tracks = self.bot.playlists.tracks(interaction.guild_id, key)
        choices: list[app_commands.Choice[str]] = []
        for track in tracks:
            haystack = f"{track.artist} {track.title}".lower()
            if current and current.lower() not in haystack:
                continue
            choices.append(
                app_commands.Choice(
                    name=f"{track.label[:80]} ({track.duration_label})",
                    value=track.id,
                )
            )
            if len(choices) >= 25:
                break
        return choices

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await self._error(interaction, "Нужны права администратора для этой команды.")
            return
        log.error("Playlist command error", exc_info=error)
        await self._error(interaction, "Не удалось выполнить команду.")


async def setup(bot: BibaBot) -> None:
    await bot.add_cog(PlaylistCog(bot))
