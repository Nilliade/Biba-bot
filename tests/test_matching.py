from __future__ import annotations

from biba.game.matching import score_guess
from biba.models import Track
from biba.text import clean_title, normalize


def track(title: str, artist: str = "") -> Track:
    return Track(id="x", title=title, artist=artist, duration=200, url="u")


def test_exact_title_matches() -> None:
    result = score_guess("Smells Like Teen Spirit", track("Smells Like Teen Spirit", "Nirvana"))
    assert result.correct
    assert result.score >= 99


def test_case_punctuation_and_spaces() -> None:
    result = score_guess(
        "  smells   LIKE teen spirit!!  ", track("Smells Like Teen Spirit", "Nirvana")
    )
    assert result.correct


def test_artist_and_title() -> None:
    result = score_guess(
        "Nirvana — Smells Like Teen Spirit", track("Smells Like Teen Spirit", "Nirvana")
    )
    assert result.correct


def test_small_typo_matches() -> None:
    result = score_guess("smels like teen spirit", track("Smells Like Teen Spirit", "Nirvana"))
    assert result.correct


def test_wrong_song_does_not_match() -> None:
    result = score_guess("Billie Jean", track("Smells Like Teen Spirit", "Nirvana"))
    assert not result.correct


def test_artist_only_does_not_match() -> None:
    result = score_guess("Nirvana", track("Smells Like Teen Spirit", "Nirvana"))
    assert not result.correct


def test_russian_title_matches() -> None:
    result = score_guess("пачка сигарет", track("Пачка сигарет", "Виктор Цой"))
    assert result.correct


def test_russian_artist_and_title_match() -> None:
    result = score_guess("Цой — Пачка сигарет", track("Пачка сигарет", "Виктор Цой"))
    assert result.correct


def test_transliteration_matches() -> None:
    result = score_guess("pachka sigaret", track("Пачка сигарет", "Виктор Цой"))
    assert result.correct


def test_yo_is_normalized() -> None:
    assert normalize("Ёлка") == normalize("Елка")


def test_official_video_suffix_is_cleaned() -> None:
    assert clean_title("Song (Official Video)") == "Song"
    assert clean_title("Song [Lyrics]") == "Song"
    assert clean_title("Song — Official Audio") == "Song"


def test_empty_inputs() -> None:
    assert not score_guess("", track("Song")).correct
    assert not score_guess("Song", None).correct
