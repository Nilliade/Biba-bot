"""Музыкальный плеер: очередь, повторы, перемешивание и режим 24/7."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from biba.models import Track
from biba.music.embeds import now_playing_embed
from biba.music.player import VoiceManager
from biba.music.resolver import TrackResolver

if TYPE_CHECKING:  # pragma: no cover
    from biba.bot import BibaBot
    from biba.database import MusicSettingsRepo

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 60.0
MAX_QUEUE = 200
WATCH_INTERVAL = 0.5


class MusicError(Exception):
    """Ошибка, которую можно показать пользователю."""


@dataclass
class QueuedTrack:
    """Трек в очереди и тот, кто его заказал."""

    track: Track
    requested_by: int


@dataclass
class PlayResult:
    """Итог добавления треков в очередь."""

    track: Track
    position: int = 0  # 0 — играет прямо сейчас, иначе номер в очереди
    queue_size: int = 0


@dataclass
class GuildPlayer:
    """Состояние плеера на одном сервере."""

    guild_id: int
    queue: deque[QueuedTrack] = field(default_factory=deque)
    current: QueuedTrack | None = None
    loop: str = "off"  # off | track | queue
    shuffle: bool = False
    volume: int = 70
    channel_id: int | None = None
    voice_channel_id: int | None = None
    suspended: bool = False
    started_at: float = 0.0
    token: int = 0

    def add(self, track: Track, user_id: int) -> int:
        if len(self.queue) >= MAX_QUEUE:
            raise MusicError(f"Очередь заполнена (максимум {MAX_QUEUE} треков).")
        self.queue.append(QueuedTrack(track=track, requested_by=user_id))
        return len(self.queue)

    @property
    def is_empty(self) -> bool:
        return self.current is None and not self.queue

    @property
    def total_duration(self) -> int:
        tracks = [item.track for item in self.queue]
        if self.current is not None:
            tracks.insert(0, self.current.track)
        return sum(int(track.duration or 0) for track in tracks)

    def apply_shuffle(self) -> None:
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)


class MusicService:
    """Очередь воспроизведения и связь с голосовым каналом.

    Плеер ничего не знает о slash-командах: cog только вызывает его методы
    и показывает результат. Это позволяет тестировать очередь без Discord.
    """

    def __init__(
        self,
        bot: BibaBot,
        *,
        resolver: TrackResolver | None = None,
        voice: VoiceManager | None = None,
        settings: MusicSettingsRepo | None = None,
    ) -> None:
        self.bot = bot
        self.resolver = resolver if resolver is not None else bot.resolver
        self.voice = voice if voice is not None else bot.voice
        self.settings = settings if settings is not None else bot.music_settings
        self._players: dict[int, GuildPlayer] = {}
        self._tasks: set[asyncio.Task] = set()
        self._idle_tasks: dict[int, asyncio.Task] = {}
        self._closing = False

    # -- служебное -------------------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _guild(self, guild_id: int) -> discord.Guild | None:
        return self.bot.get_guild(guild_id)

    def player(self, guild_id: int) -> GuildPlayer:
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(guild_id=guild_id)
            try:
                player.volume = self.settings.get(guild_id).volume
            except Exception:  # noqa: BLE001
                player.volume = 70
            self._players[guild_id] = player
        return player

    def is_playing(self, guild: discord.Guild) -> bool:
        vc = getattr(guild, "voice_client", None)
        if vc is not None and getattr(vc, "is_connected", lambda: False)():
            if vc.is_playing() or vc.is_paused():
                return True
        # Сухой режим (VOICE_ENABLED=false): голоса нет, ориентируемся на плеер.
        player = self._players.get(getattr(guild, "id", 0))
        return bool(player and player.current is not None and not player.suspended)

    # -- подключение -----------------------------------------------------
    async def connect(self, guild: discord.Guild, channel: discord.abc.Connectable | None) -> bool:
        if channel is None:
            return False
        ok = await self.voice.connect(guild, channel)
        if ok:
            player = self.player(guild.id)
            player.voice_channel_id = getattr(channel, "id", None)
            self._cancel_idle(guild.id)
        return ok

    # -- очередь ---------------------------------------------------------
    async def play_query(
        self,
        guild: discord.Guild,
        channel: discord.abc.Connectable | None,
        query: str,
        user_id: int,
        *,
        text_channel_id: int | None = None,
    ) -> PlayResult:
        track = await self.resolver.resolve(query)
        return await self.play_tracks(
            guild, channel, [track], user_id, text_channel_id=text_channel_id
        )

    async def play_tracks(
        self,
        guild: discord.Guild,
        channel: discord.abc.Connectable | None,
        tracks: list[Track],
        user_id: int,
        *,
        text_channel_id: int | None = None,
    ) -> PlayResult:
        if not tracks:
            raise MusicError("Треки не найдены.")
        player = self.player(guild.id)
        if text_channel_id:
            player.channel_id = int(text_channel_id)
        if not await self.connect(guild, channel):
            raise MusicError("Не удалось подключиться к голосовому каналу.")
        playing = self.is_playing(guild)
        position = len(player.queue) + (1 if player.current is not None else 0)
        for track in tracks:
            player.add(track, user_id)
        if player.shuffle:
            player.apply_shuffle()
        if not playing:
            await self._play_current(guild)
            position = 0
        return PlayResult(track=tracks[0], position=position, queue_size=len(player.queue))

    async def skip(self, guild: discord.Guild) -> bool:
        player = self.player(guild.id)
        if player.is_empty:
            return False
        player.token += 1
        self.voice.stop(guild.id)
        player.current = None
        await self._advance(guild.id, force_next=True)
        return True

    async def stop(self, guild: discord.Guild, *, disconnect: bool = True) -> int:
        player = self.player(guild.id)
        count = len(player.queue) + (1 if player.current is not None else 0)
        player.token += 1
        player.queue.clear()
        player.current = None
        self.voice.stop(guild.id)
        self._cancel_idle(guild.id)
        if disconnect:
            await self.voice.disconnect(guild.id)
        self._players.pop(guild.id, None)
        return count

    def pause(self, guild: discord.Guild) -> bool:
        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected() or not vc.is_playing():
            return False
        vc.pause()
        return True

    def resume(self, guild: discord.Guild) -> bool:
        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected() or not vc.is_paused():
            return False
        vc.resume()
        return True

    async def set_volume(self, guild: discord.Guild, value: int) -> int:
        value = max(0, min(200, int(value)))
        player = self.player(guild.id)
        player.volume = value
        self.voice.set_volume(guild, value)
        self.settings.update(guild.id, volume=value)
        return value

    def set_loop(self, guild_id: int, mode: str) -> str:
        if mode not in {"off", "track", "queue"}:
            raise MusicError("Режим повтора: off, track или queue.")
        self.player(guild_id).loop = mode
        return mode

    def toggle_shuffle(self, guild_id: int) -> bool:
        player = self.player(guild_id)
        player.shuffle = not player.shuffle
        if player.shuffle:
            player.apply_shuffle()
        return player.shuffle

    # -- воспроизведение --------------------------------------------------
    async def _play_current(self, guild: discord.Guild) -> None:
        player = self.player(guild.id)
        if player.current is None:
            if not player.queue:
                await self._on_queue_empty(guild)
                return
            player.current = player.queue.popleft()
        current = player.current
        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected():
            channel = (
                guild.get_channel(player.voice_channel_id) if player.voice_channel_id else None
            )
            if channel is None or not await self.connect(guild, channel):
                player.current = None
                return
        try:
            stream = await self.resolver.stream_url(current.track)
        except Exception as exc:  # noqa: BLE001 - пробуем следующий трек
            log.warning("Stream resolve failed for %s: %s", current.track.id, exc)
            player.current = None
            await self._advance(guild.id, force_next=True)
            return
        player.token += 1
        player.started_at = time.monotonic()
        played = await self.voice.play(guild, stream, offset=0, volume=player.volume)
        if not played:
            player.current = None
            await self._advance(guild.id, force_next=True)
            return
        self._spawn(self._watch(guild.id, player.token))
        await self._announce(guild, current)

    async def _watch(self, guild_id: int, token: int) -> None:
        """Ждёт конца трека. Опрос надёжнее after-колбэка: тот срабатывает
        и при ручном ``stop()``, из-за чего очередь могла бы проматываться."""
        while True:
            try:
                await asyncio.sleep(WATCH_INTERVAL)
            except asyncio.CancelledError:
                raise
            player = self._players.get(guild_id)
            if player is None or player.token != token or player.suspended:
                return
            guild = self._guild(guild_id)
            if guild is None:
                return
            vc = getattr(guild, "voice_client", None)
            if vc is None or not vc.is_connected():
                await self._advance(guild_id)
                return
            if vc.is_playing() or vc.is_paused():
                continue
            await self._advance(guild_id)
            return

    async def _advance(self, guild_id: int, *, force_next: bool = False) -> None:
        player = self._players.get(guild_id)
        if player is None or player.suspended:
            return
        guild = self._guild(guild_id)
        if guild is None:
            return
        current = player.current
        if not force_next and current is not None and player.loop == "track":
            await self._play_current(guild)
            return
        if current is not None and player.loop == "queue":
            player.queue.append(current)
        player.current = None
        player.token += 1
        if player.queue:
            await self._play_current(guild)
        else:
            await self._on_queue_empty(guild)

    async def _announce(self, guild: discord.Guild, item: QueuedTrack) -> None:
        player = self.player(guild.id)
        channel_id = player.channel_id
        if not channel_id:
            return
        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=now_playing_embed(item))
        except discord.HTTPException:
            log.debug("Cannot announce track in guild %s", guild.id)

    async def _on_queue_empty(self, guild: discord.Guild) -> None:
        player = self.player(guild.id)
        if player.suspended:
            return
        try:
            if self.settings.get(guild.id).twenty_four_seven:
                return
        except Exception:  # noqa: BLE001
            return
        self._schedule_idle(guild.id)

    def _schedule_idle(self, guild_id: int) -> None:
        self._cancel_idle(guild_id)
        self._idle_tasks[guild_id] = self._spawn(self._disconnect_if_idle(guild_id))

    def _cancel_idle(self, guild_id: int) -> None:
        task = self._idle_tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    async def _disconnect_if_idle(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        player = self._players.get(guild_id)
        if player is None or not player.is_empty or player.suspended:
            return
        try:
            if self.settings.get(guild_id).twenty_four_seven:
                return
        except Exception:  # noqa: BLE001
            return
        await self.voice.disconnect(guild_id)
        self._players.pop(guild_id, None)
        self._idle_tasks.pop(guild_id, None)

    # -- пауза на время игры ---------------------------------------------
    async def pause_for_game(self, guild_id: int) -> None:
        """Останавливает музыку, сохраняя очередь, пока идёт игра."""
        player = self._players.get(guild_id)
        if player is None:
            return
        player.suspended = True
        player.token += 1
        self._cancel_idle(guild_id)
        self.voice.stop(guild_id)

    async def resume_after_game(self, guild_id: int) -> None:
        """Возвращает музыку после игры."""
        if self._closing:
            return
        player = self._players.get(guild_id)
        if player is None:
            return
        player.suspended = False
        if player.is_empty:
            return
        guild = self._guild(guild_id)
        if guild is None:
            return
        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected():
            channel = (
                guild.get_channel(player.voice_channel_id) if player.voice_channel_id else None
            )
            if channel is None or not await self.voice.connect(guild, channel):
                return
        await self._play_current(guild)

    # -- события ----------------------------------------------------------
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        player = self._players.get(guild.id)
        if player is None:
            return
        me = getattr(self.bot, "user", None)
        vc = getattr(guild, "voice_client", None)
        if vc is None or not vc.is_connected():
            return
        if me is not None and member.id == me.id:
            if after.channel is not None:
                player.voice_channel_id = after.channel.id
            return
        channel = vc.channel
        if channel is None:
            return
        try:
            twenty_four_seven = self.settings.get(guild.id).twenty_four_seven
        except Exception:  # noqa: BLE001
            twenty_four_seven = False
        if twenty_four_seven:
            return
        humans = [item for item in channel.members if not item.bot]
        if humans:
            self._cancel_idle(guild.id)
        elif player.is_empty:
            self._schedule_idle(guild.id)

    async def restore_all(self) -> None:
        """Подключается к каналам серверов с включённым режимом 24/7."""
        for guild in self.bot.guilds:
            try:
                settings = self.settings.get(guild.id)
            except Exception:  # noqa: BLE001
                continue
            if not settings.twenty_four_seven or not settings.voice_channel_id:
                continue
            channel = guild.get_channel(settings.voice_channel_id)
            if channel is None:
                continue
            await self.voice.connect(guild, channel)
            self.player(guild.id).voice_channel_id = channel.id

    async def prepare_shutdown(self) -> None:
        """Запрещает возвращать музыку: бот выключается."""
        self._closing = True

    async def close(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._idle_tasks.clear()
        self._players.clear()
