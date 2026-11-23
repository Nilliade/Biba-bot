"""Тесты контроллера: сообщения, перерисовка экранов и потеря сообщения."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import discord

from biba.database import GuildSettings
from biba.game.controller import GameController
from biba.game.engine import MelodyEngine
from biba.game.models import Phase
from tests.conftest import FakeTrackProvider, FakeWallet, create_started_game, make_tracks
from tests.test_embeds import make_game


class FakeVoice:
    def __init__(self) -> None:
        self.is_enabled = False
        self.stopped = 0
        self.disconnected = 0

    async def stop(self, guild_id: int) -> None:
        self.stopped += 1

    async def disconnect(self, guild_id: int) -> None:
        self.disconnected += 1

    async def connect(self, guild: Any, channel: Any) -> bool:
        return True


class FakeResolver:
    async def stream_url(self, track: Any) -> str | None:
        return "https://example.com/stream"


class FakeMessage:
    def __init__(self, error: Exception | None = None) -> None:
        self.edits: list[dict[str, Any]] = []
        self.error = error

    async def edit(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.edits.append(kwargs)


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.sent: list[dict[str, Any]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, **kwargs: Any) -> None:
        self.done = True
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = SimpleNamespace(sent=[])
        self.guild_id = 1


def make_controller(
    engine: MelodyEngine,
    game: Any,
    message: FakeMessage | None = None,
    *,
    bot: Any = None,
    guild: Any = None,
) -> GameController:
    controller = GameController(
        bot=bot or SimpleNamespace(),
        engine=engine,
        resolver=FakeResolver(),
        voice=FakeVoice(),
        history=SimpleNamespace(record_round=lambda **kwargs: None),
        guild=guild
        or SimpleNamespace(
            get_channel=lambda channel_id: None,
            get_member=lambda user_id: None,
            get_role=lambda role_id: None,
            me=None,
        ),
        game=game,
        settings=GuildSettings(guild_id="1"),
    )
    controller.message = message or FakeMessage()
    return controller


def make_engine() -> MelodyEngine:
    return MelodyEngine(FakeWallet(), FakeTrackProvider(make_tracks()))


def begin_betting(engine: MelodyEngine) -> Any:
    create_started_game(engine)
    game = engine.get_game(1)
    engine.choose_track(1, game.host_id, game.round.candidates[0].id)
    engine.begin_betting(1, game.host_id)
    return game


def test_edit_keeps_view_by_default() -> None:
    message = FakeMessage()
    controller = make_controller(make_engine(), make_game(), message)

    async def scenario() -> None:
        await controller._edit(discord.Embed(title="Тест"))

    asyncio.run(scenario())
    assert len(message.edits) == 1
    assert "view" not in message.edits[0]


def test_edit_removes_view_when_asked() -> None:
    message = FakeMessage()
    controller = make_controller(make_engine(), make_game(), message)

    async def scenario() -> None:
        await controller._edit(discord.Embed(title="Тест"), None)

    asyncio.run(scenario())
    assert message.edits[0]["view"] is None


def test_edit_skips_wrong_phase() -> None:
    game = make_game()
    game.phase = Phase.BETTING
    message = FakeMessage()
    controller = make_controller(make_engine(), game, message)

    async def scenario() -> None:
        await controller._edit(discord.Embed(title="Тест"), phase=Phase.LISTENING)

    asyncio.run(scenario())
    assert message.edits == []


def test_rerender_betting_keeps_buttons() -> None:
    engine = make_engine()
    game = begin_betting(engine)
    message = FakeMessage()
    controller = make_controller(engine, game, message)

    asyncio.run(controller._rerender())
    assert game.phase is Phase.BETTING
    assert len(message.edits) == 1
    assert "view" not in message.edits[0]


def test_leave_in_betting_keeps_buttons() -> None:
    engine = make_engine()
    game = begin_betting(engine)
    message = FakeMessage()
    controller = make_controller(engine, game, message)
    interaction = FakeInteraction(102)

    asyncio.run(controller.leave(interaction))
    assert 102 not in game.players
    assert len(message.edits) == 1
    assert "view" not in message.edits[0]


def test_message_lost_cancels_game() -> None:
    engine = make_engine()
    game = begin_betting(engine)
    message = FakeMessage(
        error=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Удалено")
    )
    controller = make_controller(engine, game, message)

    async def scenario() -> None:
        await controller._edit(discord.Embed(title="Тест"))
        assert controller._cleanup_task is not None
        await controller._cleanup_task

    asyncio.run(scenario())
    assert controller.closed is True
    assert engine.get_game(1) is None
    assert controller.voice.stopped >= 1


def test_full_round_until_results() -> None:
    engine = make_engine()
    game = begin_betting(engine)
    engine.place_bet(1, 101, 100)
    message = FakeMessage()
    controller = make_controller(engine, game, message)

    async def scenario() -> None:
        controller._stream_url = "https://example.com/stream"
        controller._betting_deadline = time.monotonic() + 30
        await controller._start_listening(None)
        assert game.phase is Phase.LISTENING

        await controller.handle_guess(FakeInteraction(101), game.round.track.title)
        assert game.phase is Phase.RESULTS
        controller._cancel_tasks()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert any("embed" in edit for edit in message.edits)


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.added: list[int] = []
        self.removed: list[int] = []


class FakeMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id

    async def add_roles(self, role: FakeRole, reason: str | None = None) -> None:
        role.added.append(self.id)

    async def remove_roles(self, role: FakeRole, reason: str | None = None) -> None:
        role.removed.append(self.id)


def test_host_role_is_persisted_and_cleared(tmp_path) -> None:
    from pathlib import Path

    from biba.database import Database, HostStateRepo

    db = Database(Path(tmp_path) / "host.sqlite")
    hosts = HostStateRepo(db)
    role = FakeRole(555)
    members = {100: FakeMember(100), 200: FakeMember(200)}
    guild = SimpleNamespace(
        get_channel=lambda channel_id: None,
        get_member=lambda user_id: members.get(user_id),
        get_role=lambda role_id: role if role_id == 555 else None,
        me=None,
    )
    engine = make_engine()
    game = begin_betting(engine)
    game.host_role_id = 555
    controller = make_controller(engine, game, bot=SimpleNamespace(hosts=hosts), guild=guild)

    async def scenario() -> None:
        await controller._apply_host_role(100)
        assert hosts.get(1) == (100, 555)
        assert role.added == [100]

        # Смена ведущего снимает роль с прошлого и запоминает нового.
        await controller._apply_host_role(200)
        assert role.removed == [100]
        assert hosts.get(1) == (200, 555)

        await controller._clear_host_role()
        assert role.removed == [100, 200]
        assert hosts.get(1) is None

    asyncio.run(scenario())
    db.close()
