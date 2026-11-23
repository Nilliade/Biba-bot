"""Множители, выигрыши и вознаграждение ведущего."""

from __future__ import annotations

# Порог — доля времени превью; множитель — награда за скорость.
TIME_TIERS: tuple[tuple[float, float], ...] = (
    (0.25, 3.0),
    (0.50, 2.0),
    (0.75, 1.5),
    (1.01, 1.25),
)

WRONG_GUESS_PENALTY = 0.25
MIN_MULTIPLIER = 1.0
STREAK_STEP = 0.1
STREAK_CAP = 0.5
NOBODY_GUESSED_BONUS = 100


def time_multiplier(elapsed: float, window: float) -> float:
    """Множитель за скорость без штрафов и серии."""
    if window <= 0:
        return 1.25
    ratio = max(0.0, min(1.0, float(elapsed) / float(window)))
    for threshold, multiplier in TIME_TIERS:
        if ratio <= threshold:
            return multiplier
    return TIME_TIERS[-1][1]


def streak_multiplier(streak: int) -> float:
    """+10% за каждую победу подряд, максимум +50%."""
    return 1.0 + STREAK_STEP * min(max(int(streak), 0), int(STREAK_CAP / STREAK_STEP))


def final_multiplier(
    elapsed: float,
    window: float,
    wrong_guesses: int,
    streak: int,
) -> float:
    """Итоговый множитель: скорость минус штрафы за ошибки плюс серия."""
    base = time_multiplier(elapsed, window) - WRONG_GUESS_PENALTY * max(0, int(wrong_guesses))
    base = max(MIN_MULTIPLIER, base)
    return round(base * streak_multiplier(streak), 2)


def payout_for(bet: int, multiplier: float) -> int:
    """Сумма, которую получает игрок (включая ставку)."""
    return int(round(max(0, int(bet)) * max(1.0, float(multiplier))))


def host_fee_for(total_bets: int, percent: int, *, nobody_guessed: bool) -> int:
    """Ведущий получает процент от банка и бонус, если никто не угадал."""
    safe_percent = max(0, min(100, int(percent)))
    fee = int(round(max(0, int(total_bets)) * safe_percent / 100))
    return fee + (NOBODY_GUESSED_BONUS if nobody_guessed else 0)


def multiplier_tier_label(elapsed: float, window: float) -> str:
    return f"×{time_multiplier(elapsed, window):.2f}".rstrip("0").rstrip(".")
