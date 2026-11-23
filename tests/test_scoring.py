from __future__ import annotations

import pytest

from biba.game.scoring import (
    final_multiplier,
    host_fee_for,
    multiplier_tier_label,
    payout_for,
    streak_multiplier,
    time_multiplier,
)


@pytest.mark.parametrize(
    ("elapsed", "window", "expected"),
    [
        (0, 20, 3.0),
        (5, 20, 3.0),
        (5.1, 20, 2.0),
        (10, 20, 2.0),
        (15, 20, 1.5),
        (19.9, 20, 1.25),
        (100, 20, 1.25),
    ],
)
def test_time_tiers(elapsed: float, window: float, expected: float) -> None:
    assert time_multiplier(elapsed, window) == expected


def test_window_zero() -> None:
    assert time_multiplier(10, 0) == 1.25


def test_wrong_guess_penalty() -> None:
    assert final_multiplier(1, 20, 0, 0) == 3.0
    assert final_multiplier(1, 20, 1, 0) == 2.75
    assert final_multiplier(1, 20, 2, 0) == 2.5


def test_multiplier_never_below_one() -> None:
    assert final_multiplier(19, 20, 5, 0) == 1.0


@pytest.mark.parametrize(
    ("streak", "expected"),
    [(0, 1.0), (1, 1.1), (5, 1.5), (10, 1.5)],
)
def test_streak_multiplier(streak: int, expected: float) -> None:
    assert streak_multiplier(streak) == pytest.approx(expected)


def test_final_multiplier_applies_streak() -> None:
    assert final_multiplier(1, 20, 0, 5) == pytest.approx(4.5)


def test_payout() -> None:
    assert payout_for(100, 3.0) == 300
    assert payout_for(100, 2.5) == 250
    assert payout_for(0, 3.0) == 0


def test_host_fee() -> None:
    assert host_fee_for(1000, 15, nobody_guessed=False) == 150
    assert host_fee_for(1000, 15, nobody_guessed=True) == 250
    assert host_fee_for(0, 15, nobody_guessed=False) == 0


def test_tier_label() -> None:
    assert multiplier_tier_label(1, 20) == "×3"
    assert multiplier_tier_label(19, 20) == "×1.25"
