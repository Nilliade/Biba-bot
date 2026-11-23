from __future__ import annotations

import time

import pytest

from biba.game.engine import MelodyEngine, random_candidate
from biba.game.models import GameError, Phase
from tests.conftest import FakeTrackProvider, create_started_game


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def timed_engine(wallet, tracks, clock: FakeClock) -> MelodyEngine:
    return MelodyEngine(wallet, FakeTrackProvider(tracks), clock=clock)


def begin_round(engine: MelodyEngine, *, host: int = 100) -> None:
    round_ = engine.get_game(1).round
    engine.choose_track(1, host, round_.candidates[0].id)
    engine.set_snippet(1, host, 10)
    engine.begin_betting(1, host)


# -- создание и лобби -----------------------------------------------------


def test_create_join_and_duplicate(wallet, engine) -> None:
    game = engine.create_game(
        guild_id=1, channel_id=1, voice_channel_id=1, host_id=100, starting_balance=500
    )
    assert game.phase is Phase.LOBBY
    assert game.players[100].balance == 500
    engine.join(1, 101)
    assert game.host_order == [100, 101]
    assert game.players[101].balance == 500
    with pytest.raises(GameError):
        engine.join(1, 101)
    engine.start(1)
    with pytest.raises(GameError):
        engine.join(1, 102)


def test_start_requires_two_players(wallet, engine) -> None:
    engine.create_game(guild_id=1, channel_id=1, voice_channel_id=1, host_id=100)
    with pytest.raises(GameError):
        engine.start(1)


def test_start_with_empty_playlist(wallet) -> None:
    engine = MelodyEngine(wallet, FakeTrackProvider([]))
    engine.create_game(guild_id=1, channel_id=1, voice_channel_id=1, host_id=100)
    engine.join(1, 101)
    with pytest.raises(GameError):
        engine.start(1)


def test_second_game_on_same_guild(wallet, engine) -> None:
    engine.create_game(guild_id=1, channel_id=1, voice_channel_id=1, host_id=100)
    with pytest.raises(GameError):
        engine.create_game(guild_id=1, channel_id=1, voice_channel_id=1, host_id=101)


# -- выбор ведущего -------------------------------------------------------


def test_host_pick_permissions(wallet, engine) -> None:
    create_started_game(engine)
    round_ = engine.get_game(1).round
    assert round_.host_id == 100
    with pytest.raises(GameError):
        engine.choose_track(1, 101, round_.candidates[0].id)
    with pytest.raises(GameError):
        engine.choose_track(1, 100, "does-not-exist")
    chosen = engine.choose_track(1, 100, round_.candidates[1].id)
    assert round_.track is chosen
    with pytest.raises(GameError):
        engine.set_snippet(1, 100, 7)
    assert engine.set_snippet(1, 100, 10) == 10


def test_reroll_is_limited(wallet, engine) -> None:
    create_started_game(engine)
    round_ = engine.get_game(1).round
    first = [track.id for track in round_.candidates]
    second = [track.id for track in engine.reroll(1, 100)]
    assert len(second) == 3
    assert set(first) != set(second)
    with pytest.raises(GameError):
        engine.reroll(1, 100)


# -- ставки ---------------------------------------------------------------


def test_betting_rules(wallet, engine) -> None:
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    with pytest.raises(GameError):
        engine.place_bet(1, 100, 100)  # ведущий
    with pytest.raises(GameError):
        engine.place_bet(1, 101, 5)  # меньше минимума
    with pytest.raises(GameError):
        engine.place_bet(1, 101, 5000)  # больше баланса

    assert engine.place_bet(1, 101, 100) == 100
    assert game.players[101].balance == 900
    assert wallet.escrow[(1, 101)] == 100

    # Повторная ставка возвращает старую и списывает новую.
    assert engine.place_bet(1, 101, 50) == 50
    assert game.players[101].balance == 950
    assert wallet.escrow[(1, 101)] == 50

    engine.place_bet(1, 102, 100)
    assert engine.all_bets_placed(game)
    engine.start_listening(1)
    assert game.phase is Phase.LISTENING


def test_all_bets_placed_ignores_broke_player(wallet, tracks) -> None:
    engine = MelodyEngine(wallet, FakeTrackProvider(tracks))
    create_started_game(engine, players=(101, 102))
    begin_round(engine)
    game = engine.get_game(1)
    wallet.balances[(1, 102)] = 0
    wallet.comeback_at[(1, 102)] = int(time.time())
    game.players[102].balance = 0
    assert not engine.all_bets_placed(game)
    engine.place_bet(1, 101, 100)
    assert engine.all_bets_placed(game)


def test_comeback_grant(wallet, tracks) -> None:
    engine = MelodyEngine(wallet, FakeTrackProvider(tracks))
    create_started_game(engine, players=(101,))
    wallet.balances[(1, 101)] = 0
    begin_round(engine)
    player = engine.get_game(1).players[101]
    assert player.balance == 500
    assert player.comeback == 500
    engine.place_bet(1, 101, 100)
    assert player.balance == 400


# -- угадывание -----------------------------------------------------------


def test_correct_guess_pays_multiplier_and_host_fee(wallet, tracks) -> None:
    clock = FakeClock()
    engine = timed_engine(wallet, tracks, clock)
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    track = game.round.track
    engine.place_bet(1, 101, 100)
    engine.place_bet(1, 102, 100)
    engine.start_listening(1)
    clock.advance(2)  # 2 из 10 секунд — первые 25%
    outcome = engine.submit_guess(1, 101, track.title)

    assert outcome.correct
    assert outcome.multiplier == 3.0
    assert outcome.payout == 300
    assert game.players[101].balance == 1200
    assert game.players[102].balance == 900
    assert game.players[100].balance == 1030  # +15% от банка 200
    assert game.players[101].streak == 1
    assert game.players[101].rounds_won == 1
    assert game.players[102].streak == 0
    assert game.phase is Phase.RESULTS
    assert game.round.winner_id == 101


def test_wrong_guess_penalty_applies(wallet, tracks) -> None:
    clock = FakeClock()
    engine = timed_engine(wallet, tracks, clock)
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    track = game.round.track
    engine.place_bet(1, 101, 100)
    engine.start_listening(1)
    clock.advance(1)
    wrong = engine.submit_guess(1, 101, "совсем не та песня")
    assert not wrong.correct
    assert wrong.multiplier == 2.75
    assert game.players[101].wrong_guesses == 1
    clock.advance(1.5)
    correct = engine.submit_guess(1, 101, track.title)
    assert correct.correct
    assert correct.multiplier == 2.75
    assert correct.payout == 275
    assert len(game.round.guesses) == 2


def test_guess_requires_bet_and_not_host(wallet, tracks) -> None:
    engine = timed_engine(wallet, tracks, FakeClock())
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    with pytest.raises(GameError):
        engine.submit_guess(1, 101, "song")  # нет ставки
    engine.place_bet(1, 101, 100)
    engine.start_listening(1)
    with pytest.raises(GameError):
        engine.submit_guess(1, 100, game.round.track.title)  # ведущий


def test_nobody_guesses_gives_host_bonus(wallet, tracks) -> None:
    clock = FakeClock()
    engine = timed_engine(wallet, tracks, clock)
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    engine.place_bet(1, 101, 100)
    engine.place_bet(1, 102, 100)
    engine.start_listening(1)
    clock.advance(11)
    result = engine.close_round(1)
    assert result.winner is None
    assert result.host_fee == 130  # 15% от 200 + 100
    assert game.players[100].balance == 1130
    assert game.players[101].balance == 900
    assert game.phase is Phase.RESULTS


# -- ротация ведущего и финал -------------------------------------------


def resolve_round(engine: MelodyEngine, *, winner: int, host: int) -> None:
    round_ = engine.get_game(1).round
    engine.choose_track(1, host, round_.candidates[0].id)
    engine.set_snippet(1, host, 10)
    engine.begin_betting(1, host)
    for uid in engine.get_game(1).players:
        if uid != host and uid != winner:
            engine.place_bet(1, uid, 100)
    engine.place_bet(1, winner, 100)
    engine.start_listening(1)
    engine.submit_guess(1, winner, engine.get_game(1).round.track.title)


def test_host_rotation_and_final_stats(wallet, tracks) -> None:
    clock = FakeClock()
    engine = timed_engine(wallet, tracks, clock)
    create_started_game(engine, players=(101, 102))
    game = engine.get_game(1)

    resolve_round(engine, winner=101, host=100)
    assert game.round_number == 1
    assert engine.next_round(1).host_id == 101
    resolve_round(engine, winner=102, host=101)
    assert engine.next_round(1).host_id == 102
    resolve_round(engine, winner=101, host=102)
    assert engine.next_round(1) is None
    assert game.phase is Phase.FINISHED
    assert game.final_winner_id == 101
    assert game.players[101].rounds_won == 2
    assert game.players[102].rounds_won == 1
    assert len(wallet.recorded) == 3
    winner_record = next(r for r in wallet.recorded if r["user_id"] == 101)
    assert winner_record["won"] is True
    assert winner_record["correct"] == 2


def test_build_round_result(wallet, tracks) -> None:
    clock = FakeClock()
    engine = timed_engine(wallet, tracks, clock)
    create_started_game(engine)
    begin_round(engine)
    engine.place_bet(1, 101, 100)
    engine.start_listening(1)
    engine.submit_guess(1, 101, engine.get_game(1).round.track.title)
    result = engine.build_round_result(1)
    assert result.winner.user_id == 101
    assert result.payout == 300
    assert result.total_bets == 100
    assert result.host_fee == 15


def test_random_candidate(wallet, tracks, engine) -> None:
    create_started_game(engine)
    round_ = engine.get_game(1).round
    assert random_candidate(round_) in round_.candidates
    assert random_candidate(type("R", (), {"candidates": []})()) is None


# -- отмена и выход -------------------------------------------------------


def test_cancel_refunds_bets(wallet, engine) -> None:
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    engine.place_bet(1, 101, 100)
    engine.cancel(1, "тест")
    assert game.phase is Phase.FINISHED
    assert game.cancelled
    assert game.players[101].balance == 1000
    assert not wallet.escrow


def test_host_leaving_aborts_round_and_refunds(wallet, engine) -> None:
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    engine.place_bet(1, 101, 100)
    engine.place_bet(1, 102, 100)
    engine.leave(1, 100)
    assert game.phase is Phase.RESULTS
    assert game.round.aborted
    assert game.players[101].balance == 1000
    assert game.players[102].balance == 1000
    assert game.next_host_id == 101
    assert engine.next_round(1).host_id == 101


def test_leave_below_two_players_cancels(wallet, engine) -> None:
    create_started_game(engine, players=(101,))
    begin_round(engine)
    game = engine.get_game(1)
    engine.place_bet(1, 101, 100)
    engine.leave(1, 101)
    assert game.phase is Phase.FINISHED
    assert game.cancelled
    assert wallet.balance(1, 101) == 1000


def test_leave_refunds_own_bet(wallet, engine) -> None:
    create_started_game(engine)
    begin_round(engine)
    game = engine.get_game(1)
    engine.place_bet(1, 101, 100)
    engine.leave(1, 101)
    assert wallet.balance(1, 101) == 1000
    assert 101 not in game.round.bets
    assert game.phase is Phase.BETTING
