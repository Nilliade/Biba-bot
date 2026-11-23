from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from biba.database import (
    Database,
    HistoryRepo,
    HostStateRepo,
    PlaylistRepo,
    SettingsRepo,
    WalletRepo,
)
from biba.models import Track


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite")
    yield database
    database.close()


def test_migrations_create_all_tables(db: Database) -> None:
    with db.reading() as conn:
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    assert {
        "guild_settings",
        "playlists",
        "melody_settings",
        "melody_wallets",
        "melody_escrow",
        "melody_rounds",
        "melody_host_state",
    } <= names
    assert version >= 3


def test_host_state_roundtrip(db: Database) -> None:
    hosts = HostStateRepo(db)
    assert hosts.get(1) is None
    assert hosts.all() == []

    hosts.set(1, 100, 555)
    assert hosts.get(1) == (100, 555)
    hosts.set(1, 200, 555)
    assert hosts.get(1) == (200, 555)
    assert hosts.all() == [(1, 200, 555)]

    hosts.set(2, 300, 777)
    assert hosts.all() == [(1, 200, 555), (2, 300, 777)]

    hosts.clear(1)
    assert hosts.get(1) is None
    assert hosts.get(2) == (300, 777)


def test_settings_defaults_and_update(db: Database) -> None:
    repo = SettingsRepo(db)
    settings = repo.get(123)
    assert settings.guild_id == "123"
    assert settings.rounds == 5
    assert settings.snippet_sec == 15
    assert settings.min_bet == 10
    assert settings.playlist_key == "melody"

    updated = repo.update(123, rounds=7, snippet_sec=20, playlist_key="My Hits")
    assert updated.rounds == 7
    assert updated.snippet_sec == 20
    assert updated.playlist_key == "my_hits"
    assert repo.get(123).rounds == 7

    with pytest.raises(ValueError):
        repo.update(123, snippet_sec=7)
    with pytest.raises(ValueError):
        repo.update(123, rounds=0)
    with pytest.raises(ValueError):
        repo.update(123, min_bet=2000, starting_balance=1000)


def test_wallet_lifecycle(db: Database) -> None:
    wallet = WalletRepo(db)
    assert wallet.ensure(1, 10, 1000) == 1000
    assert wallet.ensure(1, 10, 999) == 1000

    assert wallet.place_bet(1, 10, 300, 1, 10) == 700
    assert wallet.balance(1, 10) == 700

    # Повторная ставка возвращает старую.
    assert wallet.place_bet(1, 10, 100, 1, 10) == 900
    assert wallet.balance(1, 10) == 900

    assert wallet.settle_bet(1, 10, 500) == 1400
    assert wallet.balance(1, 10) == 1400

    wallet.ensure(1, 11, 1000)
    wallet.place_bet(1, 11, 200, 1, 10)
    wallet.forfeit_bet(1, 11)
    assert wallet.balance(1, 11) == 800

    wallet.ensure(1, 12, 1000)
    wallet.place_bet(1, 12, 150, 1, 10)
    assert wallet.refund_bet(1, 12) == 150
    assert wallet.balance(1, 12) == 1000


def test_wallet_insufficient_funds(db: Database) -> None:
    wallet = WalletRepo(db)
    wallet.ensure(1, 10, 100)
    with pytest.raises(ValueError):
        wallet.place_bet(1, 10, 101, 1, 10)


def test_comeback(db: Database) -> None:
    wallet = WalletRepo(db)
    wallet.ensure(1, 10, 0)
    assert wallet.try_comeback(1, 10, threshold=10, grant=500, cooldown_seconds=3600) == 500
    assert wallet.balance(1, 10) == 500
    # Повторно в пределах кулдауна — ничего.
    wallet.ensure(1, 10, 0)
    assert wallet.try_comeback(1, 10, threshold=10, grant=500, cooldown_seconds=3600) == 0


def test_record_game_and_leaderboard(db: Database) -> None:
    wallet = WalletRepo(db)
    wallet.ensure(1, 10, 500)
    wallet.ensure(1, 11, 900)
    wallet.record_game(
        1, 10, won=True, correct=3, wrong=1, best_streak=2, current_streak=2, total_won=900
    )
    wallet.record_game(
        1, 11, won=False, correct=1, wrong=2, best_streak=1, current_streak=0, total_won=100
    )
    stats = wallet.stats(1, 10)
    assert stats is not None
    assert stats["games"] == 1
    assert stats["wins"] == 1
    assert stats["correct_guesses"] == 3
    assert stats["wrong_guesses"] == 1
    assert stats["best_streak"] == 2
    assert stats["total_won"] == 900

    board = wallet.leaderboard(1)
    assert [row["user_id"] for row in board] == ["11", "10"]


def test_refund_all_escrow(db: Database) -> None:
    wallet = WalletRepo(db)
    wallet.ensure(1, 10, 1000)
    wallet.ensure(1, 11, 1000)
    wallet.place_bet(1, 10, 300, 1, 10)
    wallet.place_bet(1, 11, 200, 1, 10)
    assert db.refund_all_escrow() == 500
    assert wallet.balance(1, 10) == 1000
    assert wallet.balance(1, 11) == 1000
    with db.reading() as conn:
        assert conn.execute("SELECT COUNT(*) FROM melody_escrow").fetchone()[0] == 0


def track(track_id: str, title: str) -> Track:
    return Track(
        id=track_id, title=title, artist="Artist", duration=180, url=f"https://e/{track_id}"
    )


def test_playlist_crud(db: Database) -> None:
    repo = PlaylistRepo(db)
    assert repo.tracks(1) == []
    assert repo.add_track(1, "melody", "Угадай мелодию", track("a", "Song A"), "u1")
    assert not repo.add_track(1, "melody", "Угадай мелодию", track("a", "Song A"), "u1")
    assert repo.add_track(1, "melody", "Угадай мелодию", track("b", "Song B"), "u1")
    assert [t.id for t in repo.tracks(1)] == ["a", "b"]
    assert repo.count(1) == 2
    info = repo.get(1, "melody")
    assert info is not None and info["name"] == "Угадай мелодию"
    assert repo.names(1) == ["melody"]

    removed = repo.remove_track(1, "melody", "a")
    assert removed is not None and removed.id == "a"
    assert repo.remove_track(1, "melody", "a") is None
    assert [t.id for t in repo.tracks(1)] == ["b"]

    listing = repo.list(1)
    assert listing[0]["key"] == "melody"
    assert listing[0]["count"] == 1


def test_history(db: Database) -> None:
    history = HistoryRepo(db)
    history.record_round(
        guild_id=1,
        round_number=1,
        host_id=10,
        track_title="Song",
        track_artist="Artist",
        snippet_sec=15,
        winner_id=11,
        winner_elapsed=3.2,
        winner_multiplier=3.0,
        winner_payout=300,
        total_bets=100,
        host_fee=15,
    )
    rows = history.recent(1)
    assert len(rows) == 1
    assert rows[0]["track_title"] == "Song"
    assert rows[0]["winner_id"] == "11"
    assert rows[0]["winner_multiplier"] == 3.0


def test_existing_minerea_database(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "data" / "minerea.sqlite"
    if not source.exists():  # pragma: no cover - на всякий случай
        pytest.skip("source database is missing")
    target = tmp_path / "minerea.sqlite"
    shutil.copy2(source, target)

    db = Database(target)
    with db.reading() as conn:
        guild_rows = conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone()[0]
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    db.close()
    assert guild_rows >= 1
    assert "melody_settings" in names
    assert "playlists" in names


def test_volume(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, volume) VALUES (?, ?)",
            ("1", 35),
        )
    assert db.get_volume(1) == 35
    assert db.get_volume(999) == 70
