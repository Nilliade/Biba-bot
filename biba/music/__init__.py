"""Музыкальная часть: резолвер, голос, плейлисты."""

from biba.music.player import VoiceManager
from biba.music.playlists import PlaylistTrackProvider
from biba.music.resolver import ResolveError, TrackResolver

__all__ = ["PlaylistTrackProvider", "ResolveError", "TrackResolver", "VoiceManager"]
