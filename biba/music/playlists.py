"""Адаптер плейлистов для движка игры."""

from __future__ import annotations

import random

from biba.database import PlaylistRepo
from biba.models import Track


class PlaylistTrackProvider:
    """Отдаёт случайные треки из нужного плейлиста."""

    def __init__(self, repo: PlaylistRepo) -> None:
        self.repo = repo

    def sample_tracks(
        self,
        guild_id: int,
        count: int,
        exclude: list[str] | None = None,
        *,
        playlist_key: str = "melody",
    ) -> list[Track]:
        tracks = self.repo.tracks(guild_id, playlist_key)
        if not tracks:
            return []
        excluded = set(exclude or [])
        available = [track for track in tracks if track.id not in excluded]
        if not available:
            available = tracks
        random.shuffle(available)
        return available[: max(0, int(count))]
