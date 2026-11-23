"""SQLite-хранилище: миграции, настройки, баллы, плейлисты и история.

База изначально принадлежала музыкальному боту Minerea: в ней уже есть
таблицы ``guild_settings`` и ``playlists``. Мы не меняем их, а добавляем
собственные таблицы с префиксом ``melody_``. Миграции идемпотентны и
могут выполняться на существующей базе.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from biba.models import Track

log = logging.getLogger(__name__)

ALLOWED_SNIPPETS = (5, 10, 15, 20, 30)


class InsufficientFunds(ValueError):
    """Недостаточно баллов для ставки."""

    def __init__(self, available: int) -> None:
        super().__init__(f"Недостаточно баллов. Доступно: {available}.")
        self.available = available


# ---------------------------------------------------------------------------
# Миграции
# ---------------------------------------------------------------------------

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
      guild_id TEXT PRIMARY KEY,
      twenty_four_seven INTEGER NOT NULL DEFAULT 0,
      volume INTEGER NOT NULL DEFAULT 70,
      dj_role_id TEXT,
      voice_channel_id TEXT,
      text_channel_id TEXT
    );

    CREATE TABLE IF NOT EXISTS playlists (
      guild_id TEXT NOT NULL,
      name_key TEXT NOT NULL,
      name TEXT NOT NULL,
      tracks TEXT NOT NULL,
      created_by TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (guild_id, name_key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS melody_settings (
      guild_id TEXT PRIMARY KEY,
      text_channel_id TEXT,
      voice_channel_id TEXT,
      rounds INTEGER NOT NULL DEFAULT 5,
      snippet_sec INTEGER NOT NULL DEFAULT 15,
      betting_seconds INTEGER NOT NULL DEFAULT 25,
      starting_balance INTEGER NOT NULL DEFAULT 1000,
      host_fee_percent INTEGER NOT NULL DEFAULT 15,
      min_bet INTEGER NOT NULL DEFAULT 10,
      random_offset INTEGER NOT NULL DEFAULT 1,
      host_role_id TEXT,
      playlist_key TEXT NOT NULL DEFAULT 'melody',
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS melody_wallets (
      guild_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      balance INTEGER NOT NULL DEFAULT 1000,
      games INTEGER NOT NULL DEFAULT 0,
      wins INTEGER NOT NULL DEFAULT 0,
      correct_guesses INTEGER NOT NULL DEFAULT 0,
      wrong_guesses INTEGER NOT NULL DEFAULT 0,
      total_won INTEGER NOT NULL DEFAULT 0,
      best_streak INTEGER NOT NULL DEFAULT 0,
      current_streak INTEGER NOT NULL DEFAULT 0,
      last_comeback_at INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS melody_escrow (
      guild_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      amount INTEGER NOT NULL,
      round_number INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS melody_rounds (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id TEXT NOT NULL,
      round_number INTEGER NOT NULL,
      host_id TEXT NOT NULL,
      track_title TEXT,
      track_artist TEXT,
      snippet_sec INTEGER NOT NULL,
      winner_id TEXT,
      winner_elapsed REAL,
      winner_multiplier REAL,
      winner_payout INTEGER NOT NULL DEFAULT 0,
      total_bets INTEGER NOT NULL DEFAULT 0,
      host_fee INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_melody_wallets_rank
      ON melody_wallets (guild_id, balance DESC, wins DESC);
    CREATE INDEX IF NOT EXISTS idx_melody_rounds_guild
      ON melody_rounds (guild_id, id DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS melody_host_state (
      guild_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      role_id TEXT NOT NULL,
      updated_at INTEGER NOT NULL
    );
    """,
)


class Database:
    """Потокобезопасная обёртка над sqlite3 с миграциями.

    Все операции короткие, поэтому выполняются синхронно в event loop.
    Один connection и ``RLock`` защищают от случайных пересечений.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.commit()
            self.migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= version:
                continue
            with self._lock:
                self._conn.executescript(script)
                self._conn.execute(f"PRAGMA user_version = {index}")
                self._conn.commit()
            log.info("Applied database migration %s", index)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Атомарная транзакция с блокировкой."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def get_volume(self, guild_id: int | str) -> int:
        """Громкость из существующей таблицы guild_settings (по умолчанию 70)."""
        with self.reading() as conn:
            row = conn.execute(
                "SELECT volume FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        return int(row["volume"]) if row and row["volume"] is not None else 70

    def refund_all_escrow(self) -> int:
        """Возвращает все зависшие ставки после падения бота."""
        now = int(time.time())
        refunded = 0
        with self.transaction() as conn:
            rows = conn.execute("SELECT guild_id, user_id, amount FROM melody_escrow").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE melody_wallets SET balance = balance + ?, updated_at = ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (int(row["amount"]), now, row["guild_id"], row["user_id"]),
                )
                refunded += int(row["amount"])
            if rows:
                conn.execute("DELETE FROM melody_escrow")
        if refunded:
            log.warning("Refunded %s points of interrupted bets", refunded)
        return refunded


# ---------------------------------------------------------------------------
# Настройки сервера
# ---------------------------------------------------------------------------


@dataclass
class GuildSettings:
    guild_id: str
    text_channel_id: int | None = None
    voice_channel_id: int | None = None
    rounds: int = 5
    snippet_sec: int = 15
    betting_seconds: int = 25
    starting_balance: int = 1000
    host_fee_percent: int = 15
    min_bet: int = 10
    random_offset: bool = True
    host_role_id: int | None = None
    playlist_key: str = "melody"

    @property
    def playlist_display_name(self) -> str:
        if self.playlist_key == "melody":
            return "Угадай мелодию"
        return self.playlist_key


_SETTINGS_COLUMNS = tuple(f.name for f in fields(GuildSettings) if f.name != "guild_id")
_INT_COLUMNS = {
    "text_channel_id",
    "voice_channel_id",
    "rounds",
    "snippet_sec",
    "betting_seconds",
    "starting_balance",
    "host_fee_percent",
    "min_bet",
    "host_role_id",
}
_BOOL_COLUMNS = {"random_offset"}


def _validate_settings(values: dict[str, Any]) -> None:
    rounds = int(values["rounds"])
    if not 1 <= rounds <= 20:
        raise ValueError("Количество раундов должно быть от 1 до 20.")
    snippet = int(values["snippet_sec"])
    if snippet not in ALLOWED_SNIPPETS:
        raise ValueError("Длительность превью: 5, 10, 15, 20 или 30 секунд.")
    betting = int(values["betting_seconds"])
    if not 10 <= betting <= 120:
        raise ValueError("Время на ставки должно быть от 10 до 120 секунд.")
    balance = int(values["starting_balance"])
    if not 100 <= balance <= 100_000:
        raise ValueError("Стартовый баланс должен быть от 100 до 100 000.")
    fee = int(values["host_fee_percent"])
    if not 0 <= fee <= 50:
        raise ValueError("Процент ведущего должен быть от 0 до 50.")
    min_bet = int(values["min_bet"])
    if not 1 <= min_bet <= 10_000:
        raise ValueError("Минимальная ставка должна быть от 1 до 10 000.")
    if min_bet > balance:
        raise ValueError("Минимальная ставка не может превышать стартовый баланс.")


class SettingsRepo:
    """CRUD для настроек игры на сервере."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, guild_id: int | str) -> GuildSettings:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT * FROM melody_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return GuildSettings(guild_id=str(guild_id))
        return GuildSettings(
            guild_id=str(row["guild_id"]),
            text_channel_id=(int(row["text_channel_id"]) if row["text_channel_id"] else None),
            voice_channel_id=(int(row["voice_channel_id"]) if row["voice_channel_id"] else None),
            rounds=int(row["rounds"]),
            snippet_sec=int(row["snippet_sec"]),
            betting_seconds=int(row["betting_seconds"]),
            starting_balance=int(row["starting_balance"]),
            host_fee_percent=int(row["host_fee_percent"]),
            min_bet=int(row["min_bet"]),
            random_offset=bool(row["random_offset"]),
            host_role_id=int(row["host_role_id"]) if row["host_role_id"] else None,
            playlist_key=str(row["playlist_key"]),
        )

    def update(self, guild_id: int | str, **changes: Any) -> GuildSettings:
        current = self.get(guild_id)
        values: dict[str, Any] = {name: getattr(current, name) for name in _SETTINGS_COLUMNS}
        for key, value in changes.items():
            if key not in _SETTINGS_COLUMNS:
                raise KeyError(f"Unknown setting: {key}")
            if key in _INT_COLUMNS and value is not None:
                value = int(value)
            elif key in _BOOL_COLUMNS:
                value = bool(value)
            elif value is not None:
                value = str(value)
            values[key] = value

        _validate_settings(values)
        values["playlist_key"] = (
            str(values["playlist_key"]).strip().lower().replace(" ", "_") or "melody"
        )
        values["host_role_id"] = int(values["host_role_id"]) if values["host_role_id"] else None
        values["text_channel_id"] = (
            int(values["text_channel_id"]) if values["text_channel_id"] else None
        )
        values["voice_channel_id"] = (
            int(values["voice_channel_id"]) if values["voice_channel_id"] else None
        )

        now = int(time.time())
        columns = ["guild_id", *_SETTINGS_COLUMNS, "updated_at"]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{name} = excluded.{name}" for name in _SETTINGS_COLUMNS)
        params = [
            str(guild_id),
            *(values[name] for name in _SETTINGS_COLUMNS),
            now,
        ]
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO melody_settings ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT(guild_id) DO UPDATE SET {updates}",
                params,
            )
        return self.get(guild_id)


# ---------------------------------------------------------------------------
# Баллы
# ---------------------------------------------------------------------------


class WalletRepo:
    """Баллы, ставки в эскроу и статистика игроков."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure(self, guild_id: int, user_id: int, starting_balance: int) -> int:
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO melody_wallets "
                "(guild_id, user_id, balance, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (str(guild_id), str(user_id), int(starting_balance), now, now),
            )
            row = conn.execute(
                "SELECT balance FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
        return int(row["balance"]) if row else int(starting_balance)

    def balance(self, guild_id: int, user_id: int) -> int:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT balance FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
        return int(row["balance"]) if row else 0

    def place_bet(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        round_number: int,
        min_bet: int = 1,
    ) -> int:
        """Списывает ставку в эскроу. Повторная ставка возвращает предыдущую."""
        amount = int(amount)
        if amount < min_bet:
            raise ValueError(f"Минимальная ставка — {min_bet}.")
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT balance FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            if row is None:
                raise ValueError("Сначала присоединитесь к игре.")
            escrow = conn.execute(
                "SELECT amount FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            old = int(escrow["amount"]) if escrow else 0
            available = int(row["balance"]) + old
            if amount > available:
                raise InsufficientFunds(available)
            if escrow:
                conn.execute(
                    "DELETE FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                    (str(guild_id), str(user_id)),
                )
            conn.execute(
                "UPDATE melody_wallets SET balance = balance + ?, updated_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (old - amount, now, str(guild_id), str(user_id)),
            )
            conn.execute(
                "INSERT INTO melody_escrow "
                "(guild_id, user_id, amount, round_number, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(guild_id), str(user_id), amount, int(round_number), now),
            )
            return available - amount

    def settle_bet(self, guild_id: int, user_id: int, payout: int) -> int:
        """Забирает ставку из эскроу и начисляет выигрыш."""
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            )
            conn.execute(
                "UPDATE melody_wallets SET balance = balance + ?, updated_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (int(payout), now, str(guild_id), str(user_id)),
            )
            row = conn.execute(
                "SELECT balance FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
        return int(row["balance"]) if row else 0

    def forfeit_bet(self, guild_id: int, user_id: int) -> None:
        """Проигранная ставка: просто удаляется из эскроу."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            )

    def refund_bet(self, guild_id: int, user_id: int) -> int:
        """Возвращает ставку игроку."""
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT amount FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            if row is None:
                return 0
            amount = int(row["amount"])
            conn.execute(
                "DELETE FROM melody_escrow WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            )
            conn.execute(
                "UPDATE melody_wallets SET balance = balance + ?, updated_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, now, str(guild_id), str(user_id)),
            )
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
        """Подъёмные игроку, у которого не хватает на минимальную ставку."""
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT balance, last_comeback_at FROM melody_wallets "
                "WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            if row is None:
                return 0
            if int(row["balance"]) >= int(threshold):
                return 0
            if now - int(row["last_comeback_at"]) < int(cooldown_seconds):
                return 0
            conn.execute(
                "UPDATE melody_wallets SET balance = balance + ?, "
                "last_comeback_at = ?, updated_at = ? WHERE guild_id = ? AND user_id = ?",
                (int(grant), now, now, str(guild_id), str(user_id)),
            )
        return int(grant)

    def credit(self, guild_id: int, user_id: int, amount: int) -> int:
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO melody_wallets "
                "(guild_id, user_id, balance, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "balance = balance + excluded.balance, updated_at = excluded.updated_at",
                (str(guild_id), str(user_id), int(amount), now, now),
            )
            row = conn.execute(
                "SELECT balance FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
        return int(row["balance"]) if row else 0

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
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO melody_wallets (
                  guild_id, user_id, balance, games, wins, correct_guesses,
                  wrong_guesses, total_won, best_streak, current_streak,
                  created_at, updated_at
                ) VALUES (?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                  games = games + 1,
                  wins = wins + excluded.wins,
                  correct_guesses = correct_guesses + excluded.correct_guesses,
                  wrong_guesses = wrong_guesses + excluded.wrong_guesses,
                  total_won = total_won + excluded.total_won,
                  best_streak = MAX(best_streak, excluded.best_streak),
                  current_streak = excluded.current_streak,
                  updated_at = excluded.updated_at
                """,
                (
                    str(guild_id),
                    str(user_id),
                    int(bool(won)),
                    int(correct),
                    int(wrong),
                    int(total_won),
                    int(best_streak),
                    int(current_streak),
                    now,
                    now,
                ),
            )

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT * FROM melody_wallets WHERE guild_id = ? AND games > 0 "
                "ORDER BY balance DESC, wins DESC, user_id ASC LIMIT ?",
                (str(guild_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT * FROM melody_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Плейлисты
# ---------------------------------------------------------------------------


class PlaylistRepo:
    """Работа с существующей таблицей ``playlists``.

    JSON-схема трека: ``{"id", "title", "artist", "duration", "url", "added_by"}``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def _row(self, guild_id: int, key: str) -> sqlite3.Row | None:
        with self.db.reading() as conn:
            return conn.execute(
                "SELECT * FROM playlists WHERE guild_id = ? AND name_key = ?",
                (str(guild_id), key),
            ).fetchone()

    def tracks(self, guild_id: int, key: str = "melody") -> list[Track]:
        row = self._row(guild_id, key)
        if row is None:
            return []
        try:
            raw = json.loads(row["tracks"] or "[]")
        except (TypeError, json.JSONDecodeError):
            log.warning("Broken playlist JSON in guild %s key %s", guild_id, key)
            return []
        return [Track.from_dict(item) for item in raw if isinstance(item, dict)]

    def get(self, guild_id: int, key: str) -> dict[str, Any] | None:
        row = self._row(guild_id, key)
        if row is None:
            return None
        return {
            "key": row["name_key"],
            "name": row["name"],
            "created_at": int(row["created_at"] or 0),
        }

    def add_track(
        self,
        guild_id: int,
        key: str,
        display_name: str,
        track: Track,
        added_by: str = "",
    ) -> bool:
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT tracks FROM playlists WHERE guild_id = ? AND name_key = ?",
                (str(guild_id), key),
            ).fetchone()
            if row is None:
                items: list[dict[str, Any]] = []
            else:
                try:
                    items = json.loads(row["tracks"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    items = []
            if not isinstance(items, list):
                items = []
            existing = [Track.from_dict(item) for item in items if isinstance(item, dict)]
            if any(t.id == track.id or (track.url and t.url == track.url) for t in existing):
                return False
            items.append(track.to_dict())
            payload = json.dumps(items, ensure_ascii=False)
            if row is None:
                conn.execute(
                    "INSERT INTO playlists "
                    "(guild_id, name_key, name, tracks, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(guild_id), key, display_name, payload, added_by, now),
                )
            else:
                conn.execute(
                    "UPDATE playlists SET tracks = ?, name = ? WHERE guild_id = ? AND name_key = ?",
                    (payload, display_name, str(guild_id), key),
                )
        return True

    def remove_track(self, guild_id: int, key: str, track_id: str) -> Track | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT tracks FROM playlists WHERE guild_id = ? AND name_key = ?",
                (str(guild_id), key),
            ).fetchone()
            if row is None:
                return None
            try:
                items = json.loads(row["tracks"] or "[]")
            except (TypeError, json.JSONDecodeError):
                items = []
            removed: Track | None = None
            kept: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                track = Track.from_dict(item)
                if track.id == track_id:
                    removed = track
                    continue
                kept.append(item)
            if removed is None:
                return None
            conn.execute(
                "UPDATE playlists SET tracks = ? WHERE guild_id = ? AND name_key = ?",
                (json.dumps(kept, ensure_ascii=False), str(guild_id), key),
            )
        return removed

    def list(self, guild_id: int) -> list[dict[str, Any]]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT name_key, name, tracks, created_at FROM playlists "
                "WHERE guild_id = ? ORDER BY name_key",
                (str(guild_id),),
            ).fetchall()
        result = []
        for row in rows:
            try:
                count = len(json.loads(row["tracks"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                count = 0
            result.append(
                {
                    "key": row["name_key"],
                    "name": row["name"],
                    "count": count,
                    "created_at": int(row["created_at"] or 0),
                }
            )
        return result

    def count(self, guild_id: int, key: str = "melody") -> int:
        return len(self.tracks(guild_id, key))

    def names(self, guild_id: int) -> list[str]:
        return [item["key"] for item in self.list(guild_id)]


# ---------------------------------------------------------------------------
# История раундов
# ---------------------------------------------------------------------------


@dataclass
class MusicSettings:
    """Настройки музыкального бота из таблицы ``guild_settings``."""

    guild_id: str
    twenty_four_seven: bool = False
    volume: int = 70
    dj_role_id: int | None = None
    voice_channel_id: int | None = None
    text_channel_id: int | None = None


class MusicSettingsRepo:
    """CRUD для музыкальных настроек сервера."""

    _COLUMNS = (
        "twenty_four_seven",
        "volume",
        "dj_role_id",
        "voice_channel_id",
        "text_channel_id",
    )

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, guild_id: int | str) -> MusicSettings:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return MusicSettings(guild_id=str(guild_id))
        return MusicSettings(
            guild_id=str(row["guild_id"]),
            twenty_four_seven=bool(row["twenty_four_seven"]),
            volume=int(row["volume"] or 70),
            dj_role_id=int(row["dj_role_id"]) if row["dj_role_id"] else None,
            voice_channel_id=(int(row["voice_channel_id"]) if row["voice_channel_id"] else None),
            text_channel_id=int(row["text_channel_id"]) if row["text_channel_id"] else None,
        )

    def update(self, guild_id: int | str, **changes: Any) -> MusicSettings:
        current = self.get(guild_id)
        values: dict[str, Any] = {
            "twenty_four_seven": int(current.twenty_four_seven),
            "volume": int(current.volume),
            "dj_role_id": current.dj_role_id,
            "voice_channel_id": current.voice_channel_id,
            "text_channel_id": current.text_channel_id,
        }
        for key, value in changes.items():
            if key not in values:
                raise KeyError(f"Unknown music setting: {key}")
            if key == "twenty_four_seven":
                value = int(bool(value))
            elif key == "volume":
                value = max(0, min(200, int(value)))
            elif value is not None:
                value = int(value)
            values[key] = value
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (
                  guild_id, twenty_four_seven, volume, dj_role_id,
                  voice_channel_id, text_channel_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  twenty_four_seven = excluded.twenty_four_seven,
                  volume = excluded.volume,
                  dj_role_id = excluded.dj_role_id,
                  voice_channel_id = excluded.voice_channel_id,
                  text_channel_id = excluded.text_channel_id
                """,
                (
                    str(guild_id),
                    int(values["twenty_four_seven"]),
                    int(values["volume"]),
                    str(values["dj_role_id"]) if values["dj_role_id"] is not None else None,
                    (
                        str(values["voice_channel_id"])
                        if values["voice_channel_id"] is not None
                        else None
                    ),
                    (
                        str(values["text_channel_id"])
                        if values["text_channel_id"] is not None
                        else None
                    ),
                ),
            )
        return self.get(guild_id)


class HostStateRepo:
    """Кто сейчас носит роль ведущего — чтобы снять её после перезапуска бота."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, guild_id: int, user_id: int, role_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO melody_host_state (guild_id, user_id, role_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  role_id = excluded.role_id,
                  updated_at = excluded.updated_at
                """,
                (str(guild_id), str(user_id), str(role_id), int(time.time())),
            )

    def get(self, guild_id: int) -> tuple[int, int] | None:
        with self.db.reading() as conn:
            row = conn.execute(
                "SELECT user_id, role_id FROM melody_host_state WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return None
        return int(row["user_id"]), int(row["role_id"])

    def all(self) -> list[tuple[int, int, int]]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT guild_id, user_id, role_id FROM melody_host_state ORDER BY guild_id"
            ).fetchall()
        return [(int(row["guild_id"]), int(row["user_id"]), int(row["role_id"])) for row in rows]

    def clear(self, guild_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM melody_host_state WHERE guild_id = ?", (str(guild_id),))


class HistoryRepo:
    """Короткая история раундов для команды /melody history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_round(
        self,
        *,
        guild_id: int,
        round_number: int,
        host_id: int,
        track_title: str | None,
        track_artist: str | None,
        snippet_sec: int,
        winner_id: int | None,
        winner_elapsed: float | None,
        winner_multiplier: float | None,
        winner_payout: int,
        total_bets: int,
        host_fee: int,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO melody_rounds (
                  guild_id, round_number, host_id, track_title, track_artist,
                  snippet_sec, winner_id, winner_elapsed, winner_multiplier,
                  winner_payout, total_bets, host_fee, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    int(round_number),
                    str(host_id),
                    track_title,
                    track_artist,
                    int(snippet_sec),
                    str(winner_id) if winner_id is not None else None,
                    float(winner_elapsed) if winner_elapsed is not None else None,
                    float(winner_multiplier) if winner_multiplier is not None else None,
                    int(winner_payout),
                    int(total_bets),
                    int(host_fee),
                    int(time.time()),
                ),
            )

    def recent(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.reading() as conn:
            rows = conn.execute(
                "SELECT * FROM melody_rounds WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (str(guild_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]
