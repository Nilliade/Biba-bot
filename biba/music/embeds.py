"""Эмбеды музыкального плеера."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from biba.database import MusicSettings
from biba.game.embeds import mention
from biba.models import format_duration

if TYPE_CHECKING:  # pragma: no cover
    from biba.music.service import GuildPlayer, PlayResult, QueuedTrack

COLOR_MUSIC = 0x1DB954
COLOR_NEUTRAL = 0x2B2D31


def now_playing_embed(item: QueuedTrack) -> discord.Embed:
    track = item.track
    embed = discord.Embed(
        title="▶️ Сейчас играет",
        description=f"**{track.label[:300]}**\n`{track.duration_label}`",
        color=COLOR_MUSIC,
    )
    if track.url:
        embed.url = track.url
    embed.set_footer(text=f"Заказал {mention(item.requested_by)}")
    return embed


def play_result_embed(result: PlayResult) -> discord.Embed:
    if result.position == 0:
        title = "▶️ Включаю"
        note = "Прямо сейчас"
    else:
        title = "➕ Добавлено в очередь"
        note = f"Позиция: {result.position} · в очереди: {result.queue_size}"
    embed = discord.Embed(
        title=title,
        description=f"**{result.track.label[:300]}**\n`{result.track.duration_label}`",
        color=COLOR_MUSIC,
    )
    embed.set_footer(text=note)
    return embed


def queue_embed(player: GuildPlayer, guild: discord.Guild | None, limit: int = 10) -> discord.Embed:
    lines: list[str] = []
    if player.current is not None:
        lines.append(
            f"**▶ {player.current.track.label[:80]}** · {player.current.track.duration_label}"
        )
    upcoming = list(player.queue)[:limit]
    if not upcoming and player.current is None:
        lines.append("Очередь пуста. Добавьте трек командой `/music play`.")
    for index, item in enumerate(upcoming, start=1):
        requester = mention(item.requested_by)
        lines.append(
            f"`{index}.` {item.track.label[:80]} · {item.track.duration_label} — {requester}"
        )
    if limit > 0 and len(player.queue) > limit:
        lines.append(f"… и ещё {len(player.queue) - limit}")
    total = format_duration(player.total_duration)
    mode = {"off": "выкл", "track": "трек", "queue": "очередь"}.get(player.loop, player.loop)
    embed = discord.Embed(title="📜 Очередь", description="\n".join(lines), color=COLOR_MUSIC)
    embed.add_field(name="Всего", value=str(len(player.queue) + (1 if player.current else 0)))
    embed.add_field(name="Длительность", value=total)
    embed.add_field(name="Повтор", value=mode)
    if guild is not None:
        embed.set_footer(text=f"Перемешивание: {'вкл' if player.shuffle else 'выкл'}")
    return embed


def stopped_embed(count: int) -> discord.Embed:
    return discord.Embed(
        title="⏹ Воспроизведение остановлено",
        description=f"Из очереди удалено треков: {count}.",
        color=COLOR_NEUTRAL,
    )


def volume_embed(value: int) -> discord.Embed:
    return discord.Embed(
        title="🔊 Громкость",
        description=f"Сейчас: **{value}%**",
        color=COLOR_MUSIC,
    )


def music_settings_embed(
    settings: MusicSettings, *, guild: discord.Guild | None = None
) -> discord.Embed:
    def channel_label(channel_id: int | None) -> str:
        if not channel_id:
            return "—"
        channel = guild.get_channel(channel_id) if guild is not None else None
        return channel.mention if channel is not None else f"`{channel_id}`"

    def role_label(role_id: int | None) -> str:
        if not role_id:
            return "—"
        role = guild.get_role(role_id) if guild is not None else None
        return role.mention if role is not None else f"`{role_id}`"

    embed = discord.Embed(title="⚙️ Настройки музыки", color=COLOR_MUSIC)
    embed.add_field(name="Громкость", value=f"{settings.volume}%")
    embed.add_field(name="24/7", value="вкл" if settings.twenty_four_seven else "выкл")
    embed.add_field(name="DJ-роль", value=role_label(settings.dj_role_id))
    embed.add_field(name="Голосовой канал", value=channel_label(settings.voice_channel_id))
    embed.add_field(name="Канал сообщений", value=channel_label(settings.text_channel_id))
    return embed


def playlist_started_embed(name: str, count: int) -> discord.Embed:
    return discord.Embed(
        title="📁 Плейлист в очереди",
        description=f"**{name[:100]}** — треков: {count}.",
        color=COLOR_MUSIC,
    )
