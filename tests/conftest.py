from __future__ import annotations

import random
import time

import pytest

from biba.database import InsufficientFunds
from biba.game.engine import MelodyEngine
from biba.models import Track


class FakeWallet:
    """Простой кошелёк в памяти с той же семантикой, что и SQLite-версия."""

    def __init__(self) -> None:
        self.balances: dict[tuple[int, int], int] = {}
        self.escrow: dict[tuple[int, int], int] = {}
        self.comeback_at: dict[tuple[int, int], int] = {}
        self.recorded: list[dict] = []

    def ensure(self, guild_id: int, user_id: int, starting_balance: int) -> int:
        return self.balances.setdefault((guild_id, user_id), starting_balance)

    def balance(self, guild_id: int, user_id: int) -> int:
        return self.balances.get((guild_id, user_id), 0)

    def place_bet(
        self, guild_id: int, user_id: int, amount: int, round_number: int, min_bet: int = 1
    ) -> int:
        key = (guild_id, user_id)
        if amount < min_bet:
            raise ValueError("Минимальная ставка.")
        old = self.escrow.get(key, 0)
        available = self.balances.get(key, 0) + old
        if amount > available:
            raise InsufficientFunds(available)
        self.balances[key] = available - amount
        self.escrow[key] = amount
        return self.balances[key]

    def settle_bet(self, guild_id: int, user_id: int, payout: int) -> int:
        key = (guild_id, user_id)
        self.escrow.pop(key, None)
        self.balances[key] = self.balances.get(key, 0) + payout
        return self.balances[key]

    def forfeit_bet(self, guild_id: int, user_id: int) -> None:
        self.escrow.pop((guild_id, user_id), None)

    def refund_bet(self, guild_id: int, user_id: int) -> int:
        key = (guild_id, user_id)
        amount = self.escrow.pop(key, 0)
        self.balances[key] = self.balances.get(key, 0) + amount
        return amount

    def try_comeback(
        self,
        guild_id: int,
        user_id: int,
        *,
        threshold: int,
        grant: int,
        cooldown_seconds: int,
    ) -> int:
        key = (guild_id, user_id)
        if self.balances.get(key, 0) >= threshold:
            return 0
        if time.time() - self.comeback_at.get(key, 0) < cooldown_seconds:
            return 0
        self.balances[key] = self.balances.get(key, 0) + grant
        self.comeback_at[key] = int(time.time())
        return grant

    def credit(self, guild_id: int, user_id: int, amount: int) -> int:
        key = (guild_id, user_id)
        self.balances[key] = self.balances.get(key, 0) + amount
        return self.balances[key]

    def record_game(
        self,
        guild_id: int,
        user_id: int,
        *,
        won: bool,
        correct: int,
        wrong: int,
        best_streak: int,
        current_streak: int,
        total_won: int,
    ) -> None:
        self.recorded.append(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "won": won,
                "correct": correct,
                "wrong": wrong,
                "best_streak": best_streak,
                "current_streak": current_streak,
                "total_won": total_won,
            }
        )


class FakeTrackProvider:
    def __init__(self, tracks: list[Track]) -> None:
        self.tracks = list(tracks)

    def sample_tracks(
        self,
        guild_id: int,
        count: int,
        exclude: list[str] | None = None,
        *,
        playlist_key: str = "melody",
    ) -> list[Track]:
        excluded = set(exclude or [])
        pool = [track for track in self.tracks if track.id not in excluded] or self.tracks
        return random.sample(pool, min(count, len(pool)))


def make_tracks(count: int = 10, prefix: str = "Song") -> list[Track]:
    return [
        Track(
            id=f"t{index}",
            title=f"{prefix} {index}",
            artist=f"Artist {index}",
            duration=200,
            url=f"https://example.com/{prefix}/{index}",
        )
        for index in range(count)
    ]


@pytest.fixture
def wallet() -> FakeWallet:
    return FakeWallet()


@pytest.fixture
def tracks() -> list[Track]:
    return make_tracks()


@pytest.fixture
def engine(wallet: FakeWallet, tracks: list[Track]) -> MelodyEngine:
    return MelodyEngine(wallet, FakeTrackProvider(tracks))


def create_started_game(
    engine: MelodyEngine, *, host: int = 100, players: tuple[int, ...] = (101, 102)
) -> None:
    engine.create_game(
        guild_id=1,
        channel_id=10,
        voice_channel_id=20,
        host_id=host,
        rounds=3,
        snippet_sec=15,
        betting_seconds=25,
        host_fee_percent=15,
        min_bet=10,
        starting_balance=1000,
    )
    for player in players:
        engine.join(1, player)
    engine.start(1)
