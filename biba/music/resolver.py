"""Резолвер треков через yt-dlp и кэш прямых ссылок на аудио."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from biba.models import Track, track_id_for
from biba.text import clean_title

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".m4a",
    ".aac",
    ".webm",
    ".mp4",
}

STREAM_TTL = 3 * 3600


class ResolveError(Exception):
    """Не удалось найти трек или получить аудиопоток."""


class TrackResolver:
    """Превращает запрос/ссылку в :class:`~biba.models.Track` и stream URL.

    yt-dlp работает в отдельном потоке, чтобы не блокировать event loop.
    """

    def __init__(self) -> None:
        self._stream_cache: dict[str, tuple[str, float]] = {}

    # -- публичный API --------------------------------------------------
    async def resolve(self, query: str) -> Track:
        query = (query or "").strip()
        if not query:
            raise ResolveError("Пустой запрос.")
        local = self._local_track(query)
        if local is not None:
            return local
        try:
            info = await asyncio.to_thread(self._extract, query)
        except Exception as exc:  # noqa: BLE001 - показываем пользователю
            log.warning("yt-dlp failed for %r: %s", query, exc)
            raise ResolveError(self._human_error(exc)) from exc
        track = self._parse(info, query)
        return track

    async def stream_url(self, track: Track) -> str:
        """Свежая прямая ссылка на аудио (кэшируется на 3 часа)."""
        if track.url.startswith("/") or track.url.startswith("file://"):
            return track.url
        cached = self._stream_cache.get(track.id)
        if cached and cached[1] > time.time():
            return cached[0]
        try:
            info = await asyncio.to_thread(self._extract, track.url)
        except Exception as exc:  # noqa: BLE001
            log.warning("yt-dlp stream failed for %r: %s", track.url, exc)
            raise ResolveError("Не удалось получить аудиопоток.") from exc
        url = self._pick_stream_url(info)
        if not url:
            raise ResolveError("У трека не найден аудиопоток.")
        self._stream_cache[track.id] = (url, time.time() + STREAM_TTL)
        return url

    # -- внутреннее -----------------------------------------------------
    @staticmethod
    def _local_track(query: str) -> Track | None:
        try:
            path = Path(query).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            return None
        return Track(
            id=track_id_for(str(path)),
            title=path.stem.replace("_", " ").strip() or path.name,
            artist="",
            duration=0,
            url=str(path),
        )

    @staticmethod
    def _extract(query: str) -> dict:
        import yt_dlp

        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "retries": 2,
            "extractor_retries": 2,
            "default_search": "ytsearch1",
            "source_address": "0.0.0.0",
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info:
            raise ResolveError("Ничего не найдено.")
        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise ResolveError("Ничего не найдено.")
            info = entries[0]
        return info

    @staticmethod
    def _pick_stream_url(info: dict) -> str | None:
        url = info.get("url")
        if url:
            return str(url)
        formats = info.get("formats") or []
        audio = [
            fmt for fmt in formats if fmt.get("url") and fmt.get("acodec") not in (None, "none")
        ]
        if not audio:
            return None
        audio.sort(key=lambda fmt: float(fmt.get("abr") or 0), reverse=True)
        return str(audio[0]["url"])

    @staticmethod
    def _parse(info: dict, query: str) -> Track:
        title = clean_title(str(info.get("title") or "").strip())
        artist = str(
            info.get("artist") or info.get("creator") or info.get("uploader") or ""
        ).strip()
        if artist.lower().endswith(" - topic"):
            artist = artist[: -len(" - topic")].strip()

        # На YouTube название часто уже содержит «Исполнитель - Трек».
        if " - " in title:
            left, right = title.split(" - ", 1)
            left, right = left.strip(), right.strip()
            if left and right:
                if not artist:
                    artist, title = left, right
                elif left.lower() in artist.lower() or artist.lower() in left.lower():
                    title = right
        if artist and title.lower().startswith(f"{artist.lower()} - "):
            title = title[len(artist) + 3 :].strip()

        url = str(info.get("webpage_url") or query)
        if not title:
            title = url
        return Track(
            id=str(info.get("id") or track_id_for(url)),
            title=title,
            artist=artist,
            duration=int(info.get("duration") or 0),
            url=url,
        )

    @staticmethod
    def _human_error(exc: Exception) -> str:
        text = str(exc)
        lowered = text.lower()
        if "sign in" in lowered or "age-restricted" in lowered or "confirm your age" in lowered:
            return "YouTube требует подтверждение возраста для этого видео."
        if "private" in lowered:
            return "Видео приватное — выберите другой трек."
        if "geo" in lowered or "country" in lowered:
            return "Трек недоступен в вашем регионе."
        if "timeout" in lowered:
            return "Источник не ответил. Попробуйте ещё раз."
        return f"Не удалось найти трек ({text[:160]})."
