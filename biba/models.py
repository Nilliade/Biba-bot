"""Общие модели данных, не зависящие от Discord."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any


def format_duration(seconds: int | float | None) -> str:
    """Человеческая длительность трека: ``3:42`` или ``1:02:03``."""
    if not seconds or seconds <= 0:
        return "—"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def track_id_for(url: str) -> str:
    """Стабильный короткий идентификатор трека по его ссылке."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Track:
    """Аудиозапись в плейлисте.

    ``stream_url`` не сохраняется в базе: прямые ссылки на аудио протухают,
    поэтому резолвер получает их заново перед воспроизведением.
    """

    id: str
    title: str
    artist: str = ""
    duration: int = 0
    url: str = ""
    added_by: str = ""
    stream_url: str | None = None

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("stream_url", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        url = str(data.get("url") or "")
        return cls(
            id=str(data.get("id") or track_id_for(url)),
            title=str(data.get("title") or "Без названия"),
            artist=str(data.get("artist") or ""),
            duration=int(data.get("duration") or 0),
            url=url,
            added_by=str(data.get("added_by") or ""),
        )
