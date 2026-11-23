"""Подключение к голосовому каналу и воспроизведение фрагмента трека."""

from __future__ import annotations

import logging
import subprocess

import discord

log = logging.getLogger(__name__)


class VoiceManager:
    """Тонкая обёртка над discord.VoiceClient.

    ``enabled=False`` включает «сухой» режим: игра идёт без звука, что удобно
    для разработки и тестов без ffmpeg/голосового канала.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._active: dict[int, discord.VoiceClient] = {}

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    async def connect(self, guild: discord.Guild, channel: discord.abc.Connectable | None) -> bool:
        if not self.enabled:
            return True
        if channel is None:
            return False
        try:
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                vc = await channel.connect(self_deaf=True, timeout=20)  # type: ignore[union-attr]
            elif getattr(vc.channel, "id", None) != getattr(channel, "id", None):
                await vc.move_to(channel)
            self._active[guild.id] = vc
            return True
        except (TimeoutError, discord.ClientException, discord.HTTPException, OSError) as exc:
            log.warning("Voice connect failed in guild %s: %s", guild.id, exc)
            return False

    async def play(
        self,
        guild: discord.Guild,
        source: str,
        *,
        offset: int = 0,
        volume: int = 70,
    ) -> bool:
        if not self.enabled:
            return True
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            log.warning("Cannot play: no voice client in guild %s", guild.id)
            return False
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        try:
            before_options = f"-ss {max(0, int(offset))}"
            if source.startswith(("http://", "https://")):
                before_options += " -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            audio = discord.FFmpegPCMAudio(
                source,
                before_options=before_options,
                stderr=subprocess.DEVNULL,
            )
            audio = discord.PCMVolumeTransformer(audio, volume=max(0.0, min(2.0, volume / 100)))
            vc.play(audio, after=self._after_play)
            self._active[guild.id] = vc
            return True
        except (discord.ClientException, FileNotFoundError, OSError) as exc:
            log.error("Playback failed in guild %s: %s", guild.id, exc)
            return False

    def set_volume(self, guild: discord.Guild, volume: int) -> None:
        """Меняет громкость текущего воспроизведения без перезапуска."""
        vc = getattr(guild, "voice_client", None)
        source = getattr(vc, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = max(0.0, min(2.0, volume / 100))

    def stop(self, guild_id: int) -> None:
        vc = self._active.get(guild_id)
        if vc is None:
            return
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except (discord.ClientException, OSError):
            pass

    async def disconnect(self, guild_id: int) -> None:
        vc = self._active.pop(guild_id, None)
        if vc is None:
            return
        try:
            if vc.is_connected():
                await vc.disconnect(force=False)
        except (discord.ClientException, discord.HTTPException, OSError) as exc:
            log.warning("Voice disconnect failed in guild %s: %s", guild_id, exc)

    @staticmethod
    def _after_play(error: BaseException | None) -> None:
        if error is not None:
            log.warning("Playback error: %s", error)
