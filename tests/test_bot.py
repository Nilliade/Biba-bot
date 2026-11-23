from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from biba.bot import EXTENSIONS, BibaBot
from biba.config import Config


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token-123")
    monkeypatch.setenv("DEV_GUILD_ID", "12345")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    config = Config.from_env()
    assert config.token == "token-123"
    assert config.dev_guild_id == 12345
    assert config.voice_enabled is False
    assert config.log_level == "DEBUG"


def test_config_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        Config.from_env()


def test_cogs_load(tmp_path: Path) -> None:
    bot = BibaBot(
        Config(
            token="test-token",
            database_path=str(tmp_path / "bot.sqlite"),
            voice_enabled=False,
        )
    )

    async def run() -> None:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        commands = {command.name for command in bot.tree.get_commands()}
        assert {"melody", "music", "playlist", "ping", "help", "about"} <= commands
        melody = bot.tree.get_command("melody")
        music = bot.tree.get_command("music")
        playlist = bot.tree.get_command("playlist")
        assert melody is not None and music is not None and playlist is not None
        assert {
            "start",
            "join",
            "leave",
            "bet",
            "guess",
            "host",
            "next",
            "stop",
            "top",
            "profile",
            "history",
            "settings",
            "help",
        } <= {command.name for command in melody.commands}
        assert {
            "play",
            "pause",
            "resume",
            "skip",
            "stop",
            "join",
            "leave",
            "queue",
            "nowplaying",
            "volume",
            "shuffle",
            "loop",
            "247",
            "djrole",
            "settings",
        } <= {command.name for command in music.commands}
        assert {"add", "list", "show", "remove", "play"} <= {
            command.name for command in playlist.commands
        }
        assert bot.get_cog("MelodyCog") is not None
        assert bot.get_cog("MusicCog") is not None
        assert bot.get_cog("PlaylistCog") is not None
        assert bot.get_cog("GeneralCog") is not None
        assert bot.music is not None
        assert bot.music_settings is not None
        await bot.close()

    asyncio.run(run())
