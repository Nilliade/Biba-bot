"""Модели состояния игры «Угадай мелодию»."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from biba.models import Track


class GameError(Exception):
    """Ошибка, которую можно показать игроку."""


class Phase(StrEnum):
    LOBBY = "lobby"
    HOST_PICK = "host_pick"
    BETTING = "betting"
    LISTENING = "listening"
    RESULTS = "results"
    FINISHED = "finished"


@dataclass
class GuessRecord:
    user_id: int
    text: str
    correct: bool
    score: float
    elapsed: float


@dataclass
class Player:
    user_id: int
    balance: int
    bet: int = 0
    streak: int = 0
    best_streak: int = 0
    rounds_won: int = 0
    correct_guesses: int = 0
    wrong_guesses: int = 0
    total_won: int = 0
    last_guess_at: float = 0.0
    joined_at: float = 0.0
    comeback: int = 0

    @property
    def has_bet(self) -> bool:
        return self.bet > 0


@dataclass
class RoundState:
    number: int
    host_id: int
    candidates: list[Track] = field(default_factory=list)
    track: Track | None = None
    snippet_sec: int = 15
    started_at: float = 0.0
    deadline: float = 0.0
    bets: dict[int, int] = field(default_factory=dict)
    guesses: list[GuessRecord] = field(default_factory=list)
    winner_id: int | None = None
    winner_elapsed: float = 0.0
    winner_multiplier: float = 0.0
    winner_payout: int = 0
    host_fee: int = 0
    resolved: bool = False
    settled: bool = False
    aborted: bool = False
    abort_reason: str = ""

    @property
    def total_bets(self) -> int:
        return sum(self.bets.values())

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - self.started_at)


@dataclass
class GameState:
    guild_id: int
    channel_id: int
    voice_channel_id: int
    host_id: int
    rounds_total: int = 5
    default_snippet_sec: int = 15
    betting_seconds: int = 25
    host_fee_percent: int = 15
    min_bet: int = 10
    starting_balance: int = 1000
    random_offset: bool = True
    host_role_id: int | None = None
    playlist_key: str = "melody"
    players: dict[int, Player] = field(default_factory=dict)
    host_order: list[int] = field(default_factory=list)
    recent_track_ids: list[str] = field(default_factory=list)
    round_number: int = 0
    phase: Phase = Phase.LOBBY
    round: RoundState | None = None
    message_id: int | None = None
    rerolls_left: int = 1
    final_winner_id: int | None = None
    created_at: float = 0.0
    cancelled: bool = False
    cancel_reason: str = ""

    @property
    def current_round(self) -> RoundState:
        if self.round is None:
            raise RuntimeError("Round is not created")
        return self.round

    @property
    def host_player(self) -> Player | None:
        if self.round is None:
            return None
        return self.players.get(self.round.host_id)

    @property
    def next_host_id(self) -> int | None:
        """Следующий ведущий по кругу, пропуская покинувших игру."""
        if not self.host_order or not self.players:
            return None
        start = 0
        if self.round is not None:
            try:
                start = self.host_order.index(self.round.host_id) + 1
            except ValueError:
                start = 0
        for offset in range(len(self.host_order)):
            candidate = self.host_order[(start + offset) % len(self.host_order)]
            if candidate in self.players:
                return candidate
        return None

    @property
    def active_players(self) -> list[Player]:
        return list(self.players.values())

    def leaderboard(self, limit: int = 10) -> list[tuple[int, int, int, int]]:
        """Сортировка: баланс, затем победы в раундах."""
        players = sorted(
            self.players.values(),
            key=lambda p: (p.balance, p.rounds_won, -p.user_id),
            reverse=True,
        )
        return [(p.user_id, p.balance, p.streak, p.rounds_won) for p in players[:limit]]


@dataclass
class GuessOutcome:
    correct: bool
    score: float
    message: str
    winner_id: int | None = None
    multiplier: float = 0.0
    payout: int = 0
    remaining: float = 0.0


@dataclass
class RoundResult:
    round: RoundState
    winner: Player | None
    payout: int
    multiplier: float
    host_fee: int
    total_bets: int
    leaderboard: list[tuple[int, int, int, int]]
