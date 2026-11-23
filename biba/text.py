"""Текстовые утилиты: нормализация и чистка названий треков."""

from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode

# Мусор в конце названий с YouTube: "(Official Video)", "Lyrics", "4K" и т.п.
_JUNK_SUFFIX = re.compile(
    r"(?:\s*[\(\[]\s*)?"
    r"(?:official\s+(?:music\s+)?(?:video|audio|visualizer)|lyric\s+video|lyrics?|"
    r"audio|video|visualizer|hd|4k|remaster(?:ed)?|m/v|mv|live\s+version|"
    r"feat\.?\s+[^()\[\]]+|ft\.?\s+[^()\[\]]+)\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^a-z0-9\s]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Приводит текст к общему виду: lowercase, латиница, без знаков препинания."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = unidecode(text).lower()
    text = _PUNCTUATION.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return text


def clean_title(title: str) -> str:
    """Убирает рекламные хвосты из названия, сохраняя сам трек."""
    if not title:
        return ""
    cleaned = _JUNK_SUFFIX.sub("", str(title)).strip(" -–—|")
    return _SPACES.sub(" ", cleaned).strip()
