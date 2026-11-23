from __future__ import annotations

from biba.database import GuildSettings
from biba.game import embeds
from biba.game.embeds import (
    betting_embed,
    final_embed,
    help_embed,
    history_embed,
    leaderboard_embed,
    listening_embed,
    lobby_embed,
    playlist_embed,
    playlists_embed,
    profile_embed,
    round_result_embed,
    settings_embed,
    wrong_guess_embed,
)
from biba.game.models import GameState, GuessOutcome, Phase, Player, RoundResult, RoundState
from biba.models import Track


class FakeUser:
    id = 42
    name = "tester"
    display_name = "Тестер"


def make_game() -> GameState:
    game = GameState(
        guild_id=1,
        channel_id=10,
        voice_channel_id=20,
        host_id=100,
        rounds_total=3,
        default_snippet_sec=15,
        betting_seconds=25,
        host_fee_percent=15,
        min_bet=10,
        starting_balance=1000,
    )
    game.players[100] = Player(user_id=100, balance=1000)
    game.players[101] = Player(user_id=101, balance=900)
    game.players[102] = Player(user_id=102, balance=800)
    game.round = RoundState(number=1, host_id=100, snippet_sec=10)
    game.round.candidates = [
        Track(
            id=f"c{index}",
            title=f"Candidate {index}",
            artist="Artist",
            duration=180,
            url=f"c{index}",
        )
        for index in range(3)
    ]
    game.round.track = Track(id="t1", title="Song", artist="Artist", duration=200, url="u")
    game.round.bets = {101: 100, 102: 50}
    return game


def test_text_helpers() -> None:
    assert embeds.plural(1, "балл", "балла", "баллов") == "балл"
    assert embeds.plural(2, "балл", "балла", "баллов") == "балла"
    assert embeds.plural(5, "балл", "балла", "баллов") == "баллов"
    assert embeds.plural(11, "балл", "балла", "баллов") == "баллов"
    assert "1 000" in embeds.fmt_points(1000)
    assert embeds.fmt_multiplier(3.0) == "×3"
    assert embeds.fmt_multiplier(1.25) == "×1.25"
    assert embeds.progress_bar(0.5, 4) == "▰▰▱▱"


def test_lobby_embed() -> None:
    game = make_game()
    game.phase = Phase.LOBBY
    embed = lobby_embed(game, GuildSettings(guild_id="1"))
    assert embed.title == "🎵 Угадай мелодию"
    assert len(embed.fields) == 4


def test_betting_and_listening_embeds() -> None:
    game = make_game()
    betting = betting_embed(game, 20)
    assert "Сделайте ставки" in betting.title
    assert any("Множители" in field.name for field in betting.fields)

    listening = listening_embed(game, 5)
    assert "ИГРАЕТ МЕЛОДИЯ" in listening.title
    assert any("Осталось" in field.name for field in listening.fields)


def test_round_result_embed_winner() -> None:
    game = make_game()
    game.phase = Phase.RESULTS
    winner = game.players[101]
    winner.rounds_won = 1
    winner.streak = 1
    game.round.winner_id = 101
    game.round.winner_elapsed = 2.5
    game.round.winner_multiplier = 3.0
    game.round.winner_payout = 300
    game.round.host_fee = 22
    game.round.guesses.append(
        __import__("biba.game.models", fromlist=["GuessRecord"]).GuessRecord(
            user_id=101, text="Song", correct=True, score=100.0, elapsed=2.5
        )
    )
    result = RoundResult(
        round=game.round,
        winner=winner,
        payout=300,
        multiplier=3.0,
        host_fee=22,
        total_bets=150,
        leaderboard=game.leaderboard(),
    )
    embed = round_result_embed(game, result)
    assert "угадал" in embed.title
    assert "300" in embed.description


def test_round_result_embed_nobody() -> None:
    game = make_game()
    game.phase = Phase.RESULTS
    game.round.winner_id = None
    game.round.host_fee = 122
    result = RoundResult(
        round=game.round,
        winner=None,
        payout=0,
        multiplier=1.0,
        host_fee=122,
        total_bets=150,
        leaderboard=game.leaderboard(),
    )
    embed = round_result_embed(game, result)
    assert "никто не угадал" in embed.title


def test_final_and_leaderboard_embeds() -> None:
    game = make_game()
    game.phase = Phase.FINISHED
    game.round_number = 3
    game.final_winner_id = 101
    final = final_embed(game, game.leaderboard())
    assert "Игра окончена" in final.title
    assert "101" in final.description

    board = leaderboard_embed(game.leaderboard(), None)
    assert "Таблица лидеров" in board.title


def test_settings_help_profile_playlist_history() -> None:
    settings = GuildSettings(guild_id="1", rounds=4, snippet_sec=20, host_role_id=7)
    settings_embed(settings)
    help_embed()

    profile = profile_embed(None, FakeUser())  # type: ignore[arg-type]
    assert "ещё не участвовал" in profile.description

    stats = {
        "balance": 1200,
        "games": 5,
        "wins": 3,
        "correct_guesses": 9,
        "wrong_guesses": 4,
        "best_streak": 3,
        "total_won": 2400,
    }
    profile = profile_embed(stats, FakeUser())  # type: ignore[arg-type]
    assert len(profile.fields) == 8

    playlist = playlist_embed(
        "melody",
        "Угадай мелодию",
        [Track(id="a", title="A", artist="B", duration=100, url="u")],
        guild_id=1,
    )
    assert "Угадай мелодию" in playlist.title

    listing = playlists_embed([{"key": "melody", "name": "Угадай мелодию", "count": 2}])
    assert "melody" in listing.fields[0].value

    history = history_embed(
        [
            {
                "round_number": 1,
                "track_title": "Song",
                "track_artist": "Artist",
                "winner_id": "101",
                "winner_multiplier": 3.0,
            }
        ],
        None,
    )
    assert "Song" in history.fields[0].value


def test_wrong_guess_embed() -> None:
    outcome = GuessOutcome(correct=False, score=45.0, message="Нет", multiplier=2.0)
    embed = wrong_guess_embed(outcome)
    assert "Не та мелодия" in embed.title


def test_embed_size_limits() -> None:
    game = make_game()
    for user_id in range(200, 230):
        game.players[user_id] = Player(user_id=user_id, balance=1000)
    embed = lobby_embed(game, GuildSettings(guild_id="1"))
    assert len(embed) <= 6000
