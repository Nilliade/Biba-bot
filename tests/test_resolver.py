from __future__ import annotations

from biba.music.resolver import TrackResolver


def test_parse_splits_artist_and_cleans_title() -> None:
    track = TrackResolver._parse(
        {
            "title": "Artist - Song (Official Video)",
            "uploader": "Artist - Topic",
            "webpage_url": "https://example.com/watch?v=1",
            "id": "abc",
            "duration": 123,
        },
        "query",
    )
    assert track.title == "Song"
    assert track.artist == "Artist"
    assert track.duration == 123
    assert track.id == "abc"


def test_parse_keeps_unrelated_dash_title() -> None:
    track = TrackResolver._parse(
        {
            "title": "Someone - Song",
            "artist": "Other Artist",
            "webpage_url": "https://example.com/watch?v=2",
            "id": "def",
        },
        "query",
    )
    assert track.title == "Someone - Song"
    assert track.artist == "Other Artist"


def test_parse_uses_webpage_url_as_title_fallback() -> None:
    track = TrackResolver._parse({"webpage_url": "https://example.com/x", "id": "x1"}, "query")
    assert track.title == "https://example.com/x"


def test_pick_stream_url_prefers_top_level_url() -> None:
    assert TrackResolver._pick_stream_url({"url": "https://audio/1"}) == "https://audio/1"


def test_pick_stream_url_falls_back_to_formats() -> None:
    info = {
        "formats": [
            {"url": "https://audio/low", "acodec": "opus", "abr": 64},
            {"url": "https://audio/high", "acodec": "opus", "abr": 160},
        ]
    }
    assert TrackResolver._pick_stream_url(info) == "https://audio/high"
    assert TrackResolver._pick_stream_url({"formats": []}) is None
