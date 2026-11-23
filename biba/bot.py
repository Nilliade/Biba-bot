"""Точка сборки Discord-бота и общих сервисов."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from biba.config import Config
from biba.database import (
    Database,
    HistoryRepo,
    HostStateRepo,
    PlaylistRepo,
    SettingsRepo,
    WalletRepo,
)
from biba.game.embeds import error_embed
from biba.game.engine import MelodyEngine
from biba.music.player import VoiceManager
from biba.music.playlists import PlaylistTrackProvider
from biba.music.resolver import TrackResolver

log = logging.getLogger(__name__)

EXTENSIONS = (
    "biba.cogs.general",
    "biba.cogs.melody",
    "biba.cogs.playlists",
)


class BibaBot(commands.Bot):
    """Бот с длинным списком сервисов, доступных командам."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        super().__init__(
            command_prefix=config.command_prefix,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=True,
                roles=False,
                replied_user=True,
            ),
        )
        self.config = config
        self.db = Database(config.database_path)
        self.settings = SettingsRepo(self.db)
        self.wallet = WalletRepo(self.db)
        self.playlists = PlaylistRepo(self.db)
        self.history = HistoryRepo(self.db)
        self.hosts = HostStateRepo(self.db)
        self.resolver = TrackResolver()
        self.track_provider = PlaylistTrackProvider(self.playlists)
        self.engine = MelodyEngine(self.wallet, self.track_provider)
        self.voice = VoiceManager(config.voice_enabled)
        self._stale_roles_cleared = False
        self._refund_interrupted_bets()

    def _refund_interrupted_bets(self) -> None:
        """Если бот упал во время раунда, возвращаем зависшие ставки."""
        try:
            refunded = self.db.refund_all_escrow()
        except Exception:  # noqa: BLE001
            log.exception("Failed to refund interrupted bets")
        else:
            if refunded:
                log.warning("Refunded %s points of interrupted bets on startup", refunded)

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", self.config.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (%s) · %s guilds · voice=%s",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
            "on" if self.voice.is_enabled else "off",
        )
        if not self._stale_roles_cleared:
            self._stale_roles_cleared = True
            await self._clear_stale_host_roles()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="🎵 /melody start",
            ),
            status=discord.Status.online,
        )

    async def _clear_stale_host_roles(self) -> None:
        """Снимает роль ведущего, оставшуюся после падения или перезапуска."""
        try:
            rows = self.hosts.all()
        except Exception:  # noqa: BLE001
            log.exception("Failed to read stored host state")
            return
        for guild_id, user_id, role_id in rows:
            guild = self.get_guild(guild_id)
            role = guild.get_role(role_id) if guild is not None else None
            if role is None:
                self.hosts.clear(guild_id)
                continue
            member = guild.get_member(user_id) if guild is not None else None
            if member is None and guild is not None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            if member is not None:
                try:
                    await member.remove_roles(role, reason="Угадай мелодию: перезапуск бота")
                except discord.HTTPException as exc:
                    log.warning("Failed to remove stale host role in guild %s: %s", guild_id, exc)
                    continue
            self.hosts.clear(guild_id)

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Нужны права администратора для этой команды."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Слишком часто. Попробуйте через {error.retry_after:.0f} с."
        elif isinstance(error, app_commands.CheckFailure):
            message = "Команда недоступна."
        else:
            log.error("Unhandled command error", exc_info=error)
            message = "Не удалось выполнить команду."
        embed = error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Failed to send command error response")

    async def on_command_error(self, context, error) -> None:  # pragma: no cover
        log.warning("Prefix command error: %s", error)

    async def close(self) -> None:
        melody = self.get_cog("MelodyCog")
        shutdown = getattr(melody, "shutdown", None)
        if shutdown is not None:
            try:
                await shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Failed to stop active games on shutdown")
        try:
            await super().close()
        finally:
            self.db.close()


def create_bot(config: Config) -> BibaBot:
    return BibaBot(config)
