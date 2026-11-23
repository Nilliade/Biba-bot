"""Конфигурация бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Все настройки, которые можно задать через ``.env`` или окружение."""

    token: str
    database_path: str = "data/minerea.sqlite"
    dev_guild_id: int | None = None
    voice_enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    log_level: str = "INFO"
    command_prefix: str = "!"

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "Не задан DISCORD_TOKEN. Скопируйте .env.example в .env и укажите токен бота."
            )

        dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
        dev_guild = int(dev_guild_raw) if dev_guild_raw.isdigit() else None

        voice_raw = os.getenv("VOICE_ENABLED", "true").strip().lower()
        voice_enabled = voice_raw not in {"0", "false", "no", "off"}

        return cls(
            token=token,
            database_path=os.getenv("DATABASE_PATH", "data/minerea.sqlite").strip()
            or "data/minerea.sqlite",
            dev_guild_id=dev_guild,
            voice_enabled=voice_enabled,
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
        )
