"""Нечёткое сравнение ответа игрока с названием трека."""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from biba.models import Track
from biba.text import normalize

MATCH_THRESHOLD = 82.0


@dataclass(frozen=True)
class MatchResult:
    score: float
    correct: bool


def score_guess(guess: str, track: Track | None) -> MatchResult:
    """Возвращает похожесть ответа (0..100) и признак зачёта.

    Сравниваем именно с названием: ответ «Nirvana» для трека Nirvana не
    засчитывается, а вот «Nirvana — Smells Like Teen Spirit» — да.
    """
    if track is None:
        return MatchResult(0.0, False)
    normalized_guess = normalize(guess)
    normalized_title = normalize(track.title)
    if not normalized_guess or not normalized_title:
        return MatchResult(0.0, False)

    scores: list[float] = [fuzz.token_sort_ratio(normalized_guess, normalized_title)]

    # Нечёткие подстроки включаем только для ответов из двух и более слов:
    # иначе одно слово «Spirit» или «Bohemian» слишком легко проходит.
    if len(normalized_guess.split()) >= 2:
        scores.append(fuzz.token_set_ratio(normalized_guess, normalized_title) * 0.92)
        scores.append(fuzz.partial_ratio(normalized_guess, normalized_title) * 0.85)

    # Словесное покрытие названия: помогает при лишних словах в ответе.
    title_words = set(normalized_title.split())
    guess_words = set(normalized_guess.split())
    if title_words:
        coverage = len(title_words & guess_words) / len(title_words)
        scores.append(coverage * 100)

    # Ответ, полностью содержащий название, почти наверняка верный.
    if normalized_title in normalized_guess:
        scores.append(96.0)

    score = max(scores)
    return MatchResult(round(float(score), 1), score >= MATCH_THRESHOLD)
