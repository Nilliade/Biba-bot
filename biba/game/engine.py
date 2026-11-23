"""Движок игры «Угадай мелодию».

Чистая синхронная машина состояний без импорта discord.py — её можно
тестировать обычным pytest. Ввод-вывод (сообщения, голос, таймеры) живёт
в :mod:`biba.game.controller`.
"""

from __future__ import annotations

import random
import time
from typing import Protocol

from biba.game.matching import score_guess
from biba.game.models import (
    GameError,
    GameState,
    GuessOutcome,
    GuessRecord,
    Phase,
    Player,
    RoundResult,
    RoundState,
)
from biba.game.scoring import final_multiplier, host_fee_for, payout_for
from biba.models import Track

ALLOWED_SNIPPETS = (5, 10, 15, 20, 30)
CANDIDATES_PER_ROUND = 3
COMEBACK_COOLDOWN = 3600
NOBODY_GUESSED_BONUS = 100


class WalletGateway(Protocol):
    """Минимальный интерфейс кошелька, нужный движку."""

    def ensure(self, guild_id: int, user_id: int, starting_balance: int) -> int: ...

    def balance(self, guild_id: int, user_id: int) -> int: ...

    def place_bet(
        self, guild_id: int, user_id: int, amount: int, round_number: int, min_bet: int = 1
    ) -> int: ...

    def settle_bet(self, guild_id: int, user_id: int, payout: int) -> int: ...

    def forfeit_bet(self, guild_id: int, user_id: int) -> None: ...

    def refund_bet(self, guild_id: int, user_id: int) -> int: ...

    def try_comeback(
        self,
        guild_id: int,
        user_id: int,
        *,
        threshold: int,
        grant: int,
        cooldown_seconds: int,
    ) -> int: ...

    def credit(self, guild_id: int, user_id: int, amount: int) -> int: ...

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
    ) -> None: ...


class TrackProvider(Protocol):
    """Источник треков для раундов."""

    def sample_tracks(
        self,
        guild_id: int,
        count: int,
        exclude: list[str] | None = None,
        *,
        playlist_key: str = "melody",
    ) -> list[Track]: ...


class MelodyEngine:
    """Состояние одной игры на сервер и переходы между фазами."""

    def __init__(
        self,
        wallet: WalletGateway,
        tracks: TrackProvider,
        *,
        clock=time.monotonic,
    ) -> None:
        self.wallet = wallet
        self.tracks = tracks
        self.clock = clock
        self.games: dict[int, GameState] = {}

    # -- доступ ---------------------------------------------------------
    def get_game(self, guild_id: int) -> GameState | None:
        return self.games.get(guild_id)

    def active_game(self, guild_id: int) -> GameState | None:
        game = self.games.get(guild_id)
        if game is None or game.phase == Phase.FINISHED:
            return None
        return game

    def remove_game(self, guild_id: int) -> None:
        self.games.pop(guild_id, None)

    def _active(self, guild_id: int) -> GameState:
        game = self.games.get(guild_id)
        if game is None:
            raise GameError("Сейчас на сервере нет активной игры.")
        if game.phase == Phase.FINISHED:
            raise GameError("Игра уже завершена.")
        return game

    def _round(self, game: GameState, phase: Phase) -> RoundState:
        if game.phase != phase:
            raise GameError("Сейчас это действие недоступно.")
        if game.round is None:
            raise GameError("Раунд ещё не создан.")
        return game.round

    # -- создание и лобби ----------------------------------------------
    def create_game(
        self,
        *,
        guild_id: int,
        channel_id: int,
        voice_channel_id: int,
        host_id: int,
        rounds: int = 5,
        snippet_sec: int = 15,
        betting_seconds: int = 25,
        host_fee_percent: int = 15,
        min_bet: int = 10,
        starting_balance: int = 1000,
        host_role_id: int | None = None,
        playlist_key: str = "melody",
        random_offset: bool = True,
    ) -> GameState:
        existing = self.games.get(guild_id)
        if existing is not None and existing.phase != Phase.FINISHED:
            raise GameError("На этом сервере уже идёт игра «Угадай мелодию».")
        if rounds < 1:
            raise GameError("Количество раундов должно быть больше нуля.")
        if snippet_sec not in ALLOWED_SNIPPETS:
            raise GameError("Длительность превью: 5, 10, 15, 20 или 30 секунд.")
        if min_bet < 1:
            raise GameError("Минимальная ставка должна быть больше нуля.")
        if starting_balance < min_bet:
            raise GameError("Стартовый баланс меньше минимальной ставки.")

        balance = self.wallet.ensure(guild_id, host_id, starting_balance)
        game = GameState(
            guild_id=guild_id,
            channel_id=channel_id,
            voice_channel_id=voice_channel_id,
            host_id=host_id,
            rounds_total=int(rounds),
            default_snippet_sec=int(snippet_sec),
            betting_seconds=int(betting_seconds),
            host_fee_percent=int(host_fee_percent),
            min_bet=int(min_bet),
            starting_balance=int(starting_balance),
            random_offset=bool(random_offset),
            host_role_id=int(host_role_id) if host_role_id else None,
            playlist_key=str(playlist_key or "melody"),
            created_at=self.clock(),
        )
        game.host_order.append(host_id)
        game.players[host_id] = Player(user_id=host_id, balance=balance, joined_at=self.clock())
        self.games[guild_id] = game
        return game

    def join(self, guild_id: int, user_id: int) -> Player:
        game = self._active(guild_id)
        if game.phase != Phase.LOBBY:
            raise GameError("Игра уже началась — присоединиться нельзя.")
        if user_id in game.players:
            raise GameError("Вы уже участвуете в этой игре.")
        balance = self.wallet.ensure(guild_id, user_id, game.starting_balance)
        player = Player(user_id=user_id, balance=balance, joined_at=self.clock())
        game.players[user_id] = player
        game.host_order.append(user_id)
        return player

    def leave(self, guild_id: int, user_id: int) -> None:
        game = self._active(guild_id)
        if user_id not in game.players:
            raise GameError("Вы не участвуете в этой игре.")
        player = game.players[user_id]
        round_ = game.round

        if player.bet > 0 and round_ is not None and not round_.settled:
            self.wallet.refund_bet(guild_id, user_id)
            round_.bets.pop(user_id, None)
            player.balance = self.wallet.balance(guild_id, user_id)
            player.bet = 0

        del game.players[user_id]
        if user_id in game.host_order:
            game.host_order.remove(user_id)

        if game.phase == Phase.LOBBY:
            if not game.players:
                self.remove_game(guild_id)
                return
            if user_id == game.host_id:
                game.host_id = game.host_order[0]
            return

        if len(game.players) < 2:
            self.cancel(guild_id, "В игре осталось меньше двух игроков")
            return

        if round_ is not None and user_id == round_.host_id:
            self._abort_round(game, "Ведущий покинул игру")

    # -- старт и выбор ведущего ----------------------------------------
    def start(self, guild_id: int) -> RoundState:
        game = self._active(guild_id)
        if game.phase != Phase.LOBBY:
            raise GameError("Игра уже началась.")
        if len(game.players) < 2:
            raise GameError("Для игры нужно минимум два игрока.")
        candidates = self._candidates(game, CANDIDATES_PER_ROUND)
        if not candidates:
            raise GameError("Плейлист пуст. Добавьте треки командой /playlist add.")
        game.round_number = 1
        game.round = RoundState(
            number=1,
            host_id=game.host_id,
            candidates=candidates,
            snippet_sec=game.default_snippet_sec,
        )
        game.phase = Phase.HOST_PICK
        game.rerolls_left = 1
        return game.round

    def _candidates(self, game: GameState, count: int) -> list[Track]:
        exclude = list(game.recent_track_ids) if game.recent_track_ids else None
        tracks = self.tracks.sample_tracks(
            game.guild_id, count, exclude, playlist_key=game.playlist_key
        )
        if not tracks and exclude:
            tracks = self.tracks.sample_tracks(
                game.guild_id, count, None, playlist_key=game.playlist_key
            )
        return list(tracks[:count])

    def reroll(self, guild_id: int, user_id: int) -> list[Track]:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.HOST_PICK)
        if user_id != round_.host_id:
            raise GameError("Только ведущий может обновить варианты.")
        if game.rerolls_left <= 0:
            raise GameError("Обновление вариантов уже использовано в этом раунде.")
        exclude = [track.id for track in round_.candidates]
        exclude.extend(game.recent_track_ids)
        new_tracks = self.tracks.sample_tracks(
            game.guild_id, CANDIDATES_PER_ROUND, exclude, playlist_key=game.playlist_key
        )
        if not new_tracks:
            new_tracks = self.tracks.sample_tracks(
                game.guild_id, CANDIDATES_PER_ROUND, None, playlist_key=game.playlist_key
            )
        if not new_tracks:
            raise GameError("В плейлисте больше нет других треков.")
        game.rerolls_left -= 1
        round_.candidates = list(new_tracks[:CANDIDATES_PER_ROUND])
        round_.track = None
        return round_.candidates

    def choose_track(self, guild_id: int, user_id: int, track_id: str) -> Track:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.HOST_PICK)
        if user_id != round_.host_id:
            raise GameError("Только ведущий выбирает мелодию.")
        track = next((item for item in round_.candidates if item.id == track_id), None)
        if track is None:
            raise GameError("Этот трек недоступен. Обновите варианты.")
        round_.track = track
        return track

    def set_snippet(self, guild_id: int, user_id: int, seconds: int) -> int:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.HOST_PICK)
        if user_id != round_.host_id:
            raise GameError("Только ведущий выбирает длительность.")
        seconds = int(seconds)
        if seconds not in ALLOWED_SNIPPETS:
            raise GameError("Длительность превью: 5, 10, 15, 20 или 30 секунд.")
        round_.snippet_sec = seconds
        return seconds

    # -- ставки ---------------------------------------------------------
    def begin_betting(self, guild_id: int, user_id: int) -> RoundState:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.HOST_PICK)
        if user_id != round_.host_id:
            raise GameError("Только ведущий запускает раунд.")
        if round_.track is None:
            raise GameError("Сначала выберите мелодию.")

        round_.bets.clear()
        round_.guesses.clear()
        round_.winner_id = None
        round_.winner_elapsed = 0.0
        round_.winner_multiplier = 0.0
        round_.winner_payout = 0
        round_.host_fee = 0
        round_.resolved = False
        round_.settled = False
        round_.aborted = False
        round_.abort_reason = ""
        game.phase = Phase.BETTING

        for uid, player in game.players.items():
            player.last_guess_at = 0.0
            if uid == round_.host_id:
                continue
            player.balance = self.wallet.balance(guild_id, uid)
            player.bet = 0
            player.comeback = 0
            if player.balance < game.min_bet:
                granted = self.wallet.try_comeback(
                    guild_id,
                    uid,
                    threshold=game.min_bet,
                    grant=max(100, game.starting_balance // 2),
                    cooldown_seconds=COMEBACK_COOLDOWN,
                )
                if granted:
                    player.balance = self.wallet.balance(guild_id, uid)
                    player.comeback = granted
        return round_

    def place_bet(self, guild_id: int, user_id: int, amount: int) -> int:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.BETTING)
        player = game.players.get(user_id)
        if player is None:
            raise GameError("Вы не участвуете в этой игре.")
        if user_id == round_.host_id:
            raise GameError("Ведущий не делает ставку в своём раунде.")
        amount = int(amount)
        if amount < game.min_bet:
            raise GameError(f"Минимальная ставка — {game.min_bet} баллов.")
        if amount > player.balance + player.bet:
            raise GameError(f"Недостаточно баллов. Доступно: {player.balance + player.bet}.")
        try:
            new_balance = self.wallet.place_bet(
                guild_id, user_id, amount, round_.number, game.min_bet
            )
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        player.balance = new_balance
        player.bet = amount
        round_.bets[user_id] = amount
        return amount

    def all_bets_placed(self, game: GameState) -> bool:
        round_ = game.round
        if round_ is None or game.phase != Phase.BETTING:
            return False
        eligible = [
            uid
            for uid, player in game.players.items()
            if uid != round_.host_id and player.balance + player.bet >= game.min_bet
        ]
        if not eligible:
            return False
        return all(round_.bets.get(uid, 0) >= game.min_bet for uid in eligible)

    # -- прослушивание и угадывание ------------------------------------
    def start_listening(self, guild_id: int) -> RoundState:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.BETTING)
        if not round_.bets:
            raise GameError("Никто не сделал ставку.")
        now = self.clock()
        round_.started_at = now
        round_.deadline = now + round_.snippet_sec
        game.phase = Phase.LISTENING
        return round_

    def submit_guess(self, guild_id: int, user_id: int, text: str) -> GuessOutcome:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.LISTENING)
        player = game.players.get(user_id)
        if player is None:
            raise GameError("Вы не участвуете в этой игре.")
        if user_id == round_.host_id:
            raise GameError("Ведущий не угадывает мелодию в своём раунде.")
        if player.bet <= 0:
            raise GameError("Сначала сделайте ставку.")
        if any(record.correct and record.user_id == user_id for record in round_.guesses):
            raise GameError("Вы уже угадали эту мелодию.")
        now = self.clock()
        if now >= round_.deadline:
            raise GameError("Время вышло.")
        if now - player.last_guess_at < 1.0:
            raise GameError("Не так быстро — подождите секунду.")
        player.last_guess_at = now

        match = score_guess(text, round_.track)
        elapsed = max(0.0, now - round_.started_at)
        remaining = max(0.0, round_.deadline - now)
        if not match.correct:
            player.wrong_guesses += 1
            round_.guesses.append(
                GuessRecord(
                    user_id=user_id, text=text, correct=False, score=match.score, elapsed=elapsed
                )
            )
            multiplier = final_multiplier(
                elapsed, round_.snippet_sec, player.wrong_guesses, player.streak
            )
            return GuessOutcome(
                correct=False,
                score=match.score,
                message="Не та мелодия.",
                multiplier=multiplier,
                remaining=remaining,
            )

        multiplier = final_multiplier(
            elapsed, round_.snippet_sec, player.wrong_guesses, player.streak
        )
        payout = payout_for(player.bet, multiplier)
        self._settle_round(game, round_, winner_id=user_id, multiplier=multiplier, payout=payout)

        player.correct_guesses += 1
        player.rounds_won += 1
        player.streak += 1
        player.best_streak = max(player.best_streak, player.streak)
        player.total_won += payout
        for uid, other in game.players.items():
            if uid != round_.host_id and uid != user_id:
                other.streak = 0

        round_.guesses.append(
            GuessRecord(
                user_id=user_id, text=text, correct=True, score=match.score, elapsed=elapsed
            )
        )
        round_.winner_id = user_id
        round_.winner_elapsed = elapsed
        round_.winner_multiplier = multiplier
        round_.winner_payout = payout
        round_.resolved = True
        game.phase = Phase.RESULTS
        return GuessOutcome(
            correct=True,
            score=match.score,
            message="Верно!",
            winner_id=user_id,
            multiplier=multiplier,
            payout=payout,
            remaining=0.0,
        )

    def _settle_round(
        self,
        game: GameState,
        round_: RoundState,
        *,
        winner_id: int | None,
        multiplier: float,
        payout: int,
    ) -> None:
        total_bets = round_.total_bets
        for uid in list(round_.bets):
            if uid == winner_id:
                self.wallet.settle_bet(game.guild_id, uid, payout)
            else:
                self.wallet.forfeit_bet(game.guild_id, uid)
        host_fee = host_fee_for(total_bets, game.host_fee_percent, nobody_guessed=winner_id is None)
        self.wallet.credit(game.guild_id, round_.host_id, host_fee)
        round_.host_fee = host_fee
        round_.winner_multiplier = multiplier
        round_.winner_payout = payout
        round_.settled = True

        for uid, player in game.players.items():
            player.bet = 0
            player.balance = self.wallet.balance(game.guild_id, uid)
        host = game.players.get(round_.host_id)
        if host:
            host.balance = self.wallet.balance(game.guild_id, round_.host_id)

    def close_round(self, guild_id: int) -> RoundResult:
        game = self._active(guild_id)
        round_ = self._round(game, Phase.LISTENING)
        self._settle_round(game, round_, winner_id=None, multiplier=1.0, payout=0)
        for uid, player in game.players.items():
            if uid != round_.host_id:
                player.streak = 0
        round_.winner_id = None
        round_.resolved = True
        game.phase = Phase.RESULTS
        return RoundResult(
            round=round_,
            winner=None,
            payout=0,
            multiplier=1.0,
            host_fee=round_.host_fee,
            total_bets=round_.total_bets,
            leaderboard=game.leaderboard(),
        )

    def abort_round(self, guild_id: int, reason: str = "") -> None:
        game = self.games.get(guild_id)
        if game is None or game.phase not in (Phase.BETTING, Phase.LISTENING):
            return
        self._abort_round(game, reason)

    def _abort_round(self, game: GameState, reason: str) -> None:
        round_ = game.round
        if round_ is None:
            return
        for uid in list(round_.bets):
            self.wallet.refund_bet(game.guild_id, uid)
            player = game.players.get(uid)
            if player:
                player.balance = self.wallet.balance(game.guild_id, uid)
                player.bet = 0
        round_.bets.clear()
        round_.aborted = True
        round_.abort_reason = reason
        round_.resolved = True
        round_.settled = True
        round_.winner_id = None
        game.phase = Phase.RESULTS

    # -- следующие раунды и финал --------------------------------------
    def next_round(self, guild_id: int) -> RoundState | None:
        game = self.games.get(guild_id)
        if game is None:
            raise GameError("Сейчас на сервере нет активной игры.")
        if game.phase == Phase.FINISHED:
            return None
        if game.phase != Phase.RESULTS:
            raise GameError("Раунд ещё не завершён.")
        if game.round_number >= game.rounds_total:
            self._finish(game)
            return None

        host_id = game.next_host_id
        if host_id is None:
            self._finish(game)
            return None

        if game.round is not None and game.round.track is not None:
            game.recent_track_ids.append(game.round.track.id)
            game.recent_track_ids = game.recent_track_ids[-8:]

        candidates = self._candidates(game, CANDIDATES_PER_ROUND)
        if not candidates:
            self._finish(game)
            return None

        game.round_number += 1
        game.round = RoundState(
            number=game.round_number,
            host_id=host_id,
            candidates=candidates,
            snippet_sec=game.default_snippet_sec,
        )
        game.phase = Phase.HOST_PICK
        game.rerolls_left = 1
        return game.round

    def finish_game(self, guild_id: int) -> None:
        game = self.games.get(guild_id)
        if game is None or game.phase == Phase.FINISHED:
            return
        self._finish(game)

    def _finish(self, game: GameState) -> None:
        if game.phase == Phase.FINISHED:
            return
        game.phase = Phase.FINISHED
        players = list(game.players.values())
        if players:
            winner = max(players, key=lambda p: (p.rounds_won, p.balance))
            game.final_winner_id = winner.user_id
        for player in players:
            self.wallet.record_game(
                game.guild_id,
                player.user_id,
                won=player.user_id == game.final_winner_id,
                correct=player.correct_guesses,
                wrong=player.wrong_guesses,
                best_streak=player.best_streak,
                current_streak=player.streak,
                total_won=player.total_won,
            )

    def cancel(self, guild_id: int, reason: str = "Игра отменена") -> None:
        game = self.games.get(guild_id)
        if game is None or game.phase == Phase.FINISHED:
            return
        round_ = game.round
        if round_ is not None and not round_.settled:
            for uid in list(round_.bets):
                self.wallet.refund_bet(guild_id, uid)
                player = game.players.get(uid)
                if player:
                    player.balance = self.wallet.balance(guild_id, uid)
                    player.bet = 0
            round_.bets.clear()
        game.cancelled = True
        game.cancel_reason = reason
        game.phase = Phase.FINISHED

    # -- утилиты --------------------------------------------------------
    def build_round_result(self, guild_id: int) -> RoundResult:
        game = self.games.get(guild_id)
        if game is None or game.round is None:
            raise GameError("Раунд не найден.")
        round_ = game.round
        winner = game.players.get(round_.winner_id) if round_.winner_id is not None else None
        return RoundResult(
            round=round_,
            winner=winner,
            payout=round_.winner_payout,
            multiplier=round_.winner_multiplier,
            host_fee=round_.host_fee,
            total_bets=round_.total_bets,
            leaderboard=game.leaderboard(),
        )


def random_candidate(round_: RoundState) -> Track | None:
    """Случайный трек из вариантов (для автозапуска по таймауту)."""
    if not round_.candidates:
        return None
    return random.choice(round_.candidates)
