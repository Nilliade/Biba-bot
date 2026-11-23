"""Тесты музыкального плеера: очередь, повторы, пауза на время игры."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from biba.cogs.music import MusicCog
from biba.database import Database, MusicSettings, MusicSettingsRepo
from biba.models import Track
from biba.music.service import GuildPlayer, MusicError, MusicService, PlayResult


class FakeVoice:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._guild: Any = None
        self.played: list[str] = []
        self.stopped = 0
        self.disconnected = 0
        self.volume = 70

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    async def connect(self, guild: Any, channel: Any) -> bool:
        self._guild = guild
        return True

    async def play(self, guild: Any, source: str, *, offset: int = 0, volume: int = 70) -> bool:
        self._guild = guild
        self.played.append(source)
        guild.voice_client.playing = True
        return True

    def set_volume(self, guild: Any, volume: int) -> None:
        self.volume = volume

    def stop(self, guild_id: int) -> None:
        self.stopped += 1
        guild = getattr(self, "_guild", None)
        if guild is not None and guild.voice_client is not None:
            guild.voice_client.playing = False

    async def disconnect(self, guild_id: int) -> None:
        self.disconnected += 1


class FakeResolver:
    async def resolve(self, query: str) -> Track:
        return Track(id=query, title=query, artist="Artist", duration=100, url=f"u://{query}")

    async def stream_url(self, track: Track) -> str:
        return f"stream://{track.id}"


class FakeSettings:
    def __init__(self, **values: Any) -> None:
        self.data = MusicSettings(guild_id="1", **values)
        self.updates: list[dict[str, Any]] = []

    def get(self, guild_id: int) -> MusicSettings:
        return self.data

    def update(self, guild_id: int, **changes: Any) -> MusicSettings:
        self.updates.append(changes)
        for key, value in changes.items():
            setattr(self.data, key, value)
        return self.data


class FakeVoiceClient:
    def __init__(self) -> None:
        self.playing = False
        self.paused = False

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self.playing and not self.paused

    def is_paused(self) -> bool:
        return self.paused

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class FakeChannel:
    id = 10


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.voice_client = FakeVoiceClient()

    def get_channel(self, channel_id: int) -> None:
        return None


class FakeBot:
    def __init__(self, guilds: list[FakeGuild] | None = None) -> None:
        self._guilds = {guild.id: guild for guild in (guilds or [FakeGuild()])}
        self.user = SimpleNamespaceUser()

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self._guilds.get(guild_id)

    def get_channel(self, channel_id: int) -> None:
        return None


class SimpleNamespaceUser:
    id = 999


def make_service(
    *, voice: FakeVoice | None = None, settings: FakeSettings | None = None
) -> MusicService:
    return MusicService(
        FakeBot(),  # type: ignore[arg-type]
        resolver=FakeResolver(),  # type: ignore[arg-type]
        voice=voice or FakeVoice(),  # type: ignore[arg-type]
        settings=settings or FakeSettings(),  # type: ignore[arg-type]
    )


def test_player_queue_and_shuffle() -> None:
    player = GuildPlayer(guild_id=1)
    player.add(Track(id="a", title="A"), 1)
    player.add(Track(id="b", title="B"), 2)
    assert len(player.queue) == 2
    assert not player.is_empty

    player.apply_shuffle()
    assert sorted(item.track.id for item in player.queue) == ["a", "b"]

    player.queue.clear()
    player.current = None
    assert player.is_empty


def test_player_queue_limit() -> None:
    player = GuildPlayer(guild_id=1)
    for index in range(200):
        player.add(Track(id=str(index), title=str(index)), 1)
    with pytest.raises(MusicError):
        player.add(Track(id="extra", title="extra"), 1)


def test_play_starts_playback_and_queues() -> None:
    voice = FakeVoice()
    service = make_service(voice=voice)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        first = await service.play_query(guild, FakeChannel(), "first", 1)
        assert first.position == 0
        assert service.player(1).current is not None
        assert service.player(1).current.track.id == "first"
        assert voice.played == ["stream://first"]

        second = await service.play_query(guild, FakeChannel(), "second", 2)
        assert second.position == 1
        assert [item.track.id for item in service.player(1).queue] == ["second"]
        await service.close()

    asyncio.run(scenario())


def test_skip_moves_to_next_track() -> None:
    voice = FakeVoice()
    service = make_service(voice=voice)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        await service.play_query(guild, FakeChannel(), "first", 1)
        await service.play_query(guild, FakeChannel(), "second", 2)
        assert await service.skip(guild) is True
        assert service.player(1).current.track.id == "second"
        assert voice.played == ["stream://first", "stream://second"]
        await service.close()

    asyncio.run(scenario())


def test_loop_track_replays_current() -> None:
    voice = FakeVoice()
    service = make_service(voice=voice)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        await service.play_query(guild, FakeChannel(), "only", 1)
        service.set_loop(1, "track")
        await service._advance(1)
        assert service.player(1).current.track.id == "only"
        assert voice.played == ["stream://only", "stream://only"]
        await service.close()

    asyncio.run(scenario())


def test_loop_queue_appends_current() -> None:
    service = make_service()
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        await service.play_query(guild, FakeChannel(), "first", 1)
        await service.play_query(guild, FakeChannel(), "second", 2)
        service.set_loop(1, "queue")
        await service._advance(1)
        assert service.player(1).current.track.id == "second"
        assert [item.track.id for item in service.player(1).queue] == ["first"]
        await service.close()

    asyncio.run(scenario())


def test_game_pause_and_resume_keeps_queue() -> None:
    voice = FakeVoice()
    service = make_service(voice=voice)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        await service.play_query(guild, FakeChannel(), "first", 1)
        await service.play_query(guild, FakeChannel(), "second", 2)

        await service.pause_for_game(1)
        assert service.player(1).suspended is True
        assert voice.stopped == 1
        assert len(service.player(1).queue) == 1

        await service.resume_after_game(1)
        assert service.player(1).suspended is False
        assert service.player(1).current.track.id == "first"
        assert voice.played[-1] == "stream://first"
        await service.close()

    asyncio.run(scenario())


def test_stop_clears_queue_and_disconnects() -> None:
    voice = FakeVoice()
    service = make_service(voice=voice)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        await service.play_query(guild, FakeChannel(), "first", 1)
        await service.play_query(guild, FakeChannel(), "second", 2)
        count = await service.stop(guild)
        assert count == 2
        assert voice.disconnected == 1
        assert 1 not in service._players
        await service.close()

    asyncio.run(scenario())


def test_empty_queue_schedules_idle_disconnect() -> None:
    service = make_service()

    async def scenario() -> None:
        await service._on_queue_empty(service.bot.get_guild(1))
        assert 1 in service._idle_tasks
        await service.close()

    asyncio.run(scenario())


def test_247_keeps_voice_on_empty_queue() -> None:
    service = make_service(settings=FakeSettings(twenty_four_seven=True))

    async def scenario() -> None:
        await service._on_queue_empty(service.bot.get_guild(1))
        assert 1 not in service._idle_tasks
        await service.close()

    asyncio.run(scenario())


def test_volume_is_persisted() -> None:
    voice = FakeVoice()
    settings = FakeSettings(volume=70)
    service = make_service(voice=voice, settings=settings)
    guild = service.bot.get_guild(1)

    async def scenario() -> None:
        result = await service.set_volume(guild, 250)
        assert result == 200
        assert voice.volume == 200
        assert settings.data.volume == 200
        assert settings.updates == [{"volume": 200}]
        await service.close()

    asyncio.run(scenario())


def test_invalid_loop_mode() -> None:
    service = make_service()
    with pytest.raises(MusicError):
        service.set_loop(1, "album")


def test_shuffle_toggles() -> None:
    service = make_service()
    assert service.toggle_shuffle(1) is True
    assert service.player(1).shuffle is True
    assert service.toggle_shuffle(1) is False


def test_music_settings_repo_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "music.sqlite")
    repo = MusicSettingsRepo(db)

    default = repo.get(1)
    assert default.volume == 70
    assert default.twenty_four_seven is False

    updated = repo.update(1, volume=120, twenty_four_seven=True, dj_role_id=555)
    assert updated.volume == 120
    assert updated.twenty_four_seven is True
    assert updated.dj_role_id == 555

    again = repo.get(1)
    assert again.volume == 120
    assert again.twenty_four_seven is True
    assert again.dj_role_id == 555

    cleared = repo.update(1, dj_role_id=None)
    assert cleared.dj_role_id is None
    assert cleared.volume == 120

    with pytest.raises(KeyError):
        repo.update(1, unknown=1)
    db.close()


class CogMember:
    def __init__(self, roles: list[Any] | None = None, manage_guild: bool = False) -> None:
        self.id = 5
        self.roles = roles or []
        self.guild_permissions = SimpleNamespace(manage_guild=manage_guild)
        self.voice: SimpleNamespace | None = None


def make_voice_channel(channel_id: int = 10) -> discord.VoiceChannel:
    channel = discord.VoiceChannel.__new__(discord.VoiceChannel)
    channel.id = channel_id
    return channel


class CogGuild:
    def __init__(self, role: Any = None, channel: Any = None) -> None:
        self.id = 1
        self.me = SimpleNamespace(id=2)
        self.voice_client = None
        self._role = role
        self._channel = channel

    def get_role(self, role_id: int) -> Any:
        return self._role

    def get_channel(self, channel_id: int) -> Any:
        return self._channel


class CogInteraction:
    def __init__(self, guild: CogGuild, user: CogMember) -> None:
        self.guild = guild
        self.user = user
        self.guild_id = guild.id
        self.channel_id = 10
        self.response = FakeResponse()
        self.followup = SimpleNamespace(sent=[])
        self.followup.send = functools.partial(_followup_send, self.followup)


async def _followup_send(followup: Any, **kwargs: Any) -> None:
    followup.sent.append(kwargs)


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.sent: list[Any] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, **kwargs: Any) -> None:
        self.done = True
        self.sent.append(kwargs)

    async def defer(self, **kwargs: Any) -> None:
        self.done = True


def make_cog(settings: FakeSettings | None = None) -> MusicCog:
    bot = SimpleNamespace(
        get_cog=lambda name: None,
        music_settings=settings or FakeSettings(),
    )
    return MusicCog(bot)  # type: ignore[arg-type]


def test_cog_allows_without_dj_role() -> None:
    cog = make_cog()
    interaction = CogInteraction(CogGuild(), CogMember())
    assert asyncio.run(cog._dj_allowed(interaction)) is True


def test_cog_dj_role_blocks_member() -> None:
    role = SimpleNamespace(id=555, mention="<@&555>")
    settings = FakeSettings(dj_role_id=555)
    cog = make_cog(settings)
    interaction = CogInteraction(CogGuild(role=role), CogMember())

    async def scenario() -> None:
        allowed = await cog._dj_allowed(interaction)
        assert allowed is False
        assert "Управлять музыкой" in interaction.response.sent[0]["embed"].description

    asyncio.run(scenario())


def test_cog_dj_role_allows_admin_and_role() -> None:
    role = SimpleNamespace(id=555, mention="<@&555>")
    settings = FakeSettings(dj_role_id=555)
    cog = make_cog(settings)
    guild = CogGuild(role=role)

    async def scenario() -> None:
        assert await cog._dj_allowed(CogInteraction(guild, CogMember(manage_guild=True)))
        assert await cog._dj_allowed(CogInteraction(guild, CogMember(roles=[role])))

    asyncio.run(scenario())


def test_cog_voice_channel_falls_back_to_setting() -> None:
    channel = make_voice_channel(10)
    settings = FakeSettings(voice_channel_id=10)
    cog = make_cog(settings)
    interaction = CogInteraction(CogGuild(channel=channel), CogMember())
    assert cog._voice_channel(interaction) is channel

    member = CogMember()
    member.voice = SimpleNamespace(channel=make_voice_channel(20))
    interaction = CogInteraction(CogGuild(channel=channel), member)
    assert cog._voice_channel(interaction).id == 20


def test_cog_game_active_check() -> None:
    melody = SimpleNamespace(is_active=lambda guild_id: guild_id == 1)
    bot = SimpleNamespace(get_cog=lambda name: melody, music_settings=FakeSettings())
    cog = MusicCog(bot)  # type: ignore[arg-type]
    assert cog._game_active(1) is True
    assert cog._game_active(2) is False


def test_cog_play_command_flow() -> None:
    channel = make_voice_channel(20)
    guild = CogGuild(channel=channel)
    member = CogMember()
    member.voice = SimpleNamespace(channel=channel)
    interaction = CogInteraction(guild, member)
    interaction.channel_id = 30
    result = PlayResult(track=Track(id="a", title="A"), position=0, queue_size=0)

    async def fake_play_query(*args: Any, **kwargs: Any) -> PlayResult:
        return result

    bot = SimpleNamespace(
        get_cog=lambda name: None,
        music_settings=FakeSettings(),
        music=SimpleNamespace(play_query=fake_play_query),
    )
    cog = MusicCog(bot)  # type: ignore[arg-type]

    async def allow(*args: Any, **kwargs: Any) -> bool:
        return True

    cog._permissions_ok = allow  # type: ignore[method-assign]
    asyncio.run(cog.play.callback(cog, interaction, "query"))
    assert interaction.response.done is True
    assert interaction.followup.sent
    assert interaction.followup.sent[0]["embed"].title == "▶️ Включаю"


def test_cog_play_blocks_during_game() -> None:
    channel = make_voice_channel(20)
    member = CogMember()
    member.voice = SimpleNamespace(channel=channel)
    interaction = CogInteraction(CogGuild(channel=channel), member)
    melody = SimpleNamespace(is_active=lambda guild_id: True)
    bot = SimpleNamespace(
        get_cog=lambda name: melody,
        music_settings=FakeSettings(),
        music=SimpleNamespace(play_query=None),
    )
    cog = MusicCog(bot)  # type: ignore[arg-type]
    asyncio.run(cog.play.callback(cog, interaction, "query"))
    assert "Идёт игра" in interaction.response.sent[0]["embed"].description
