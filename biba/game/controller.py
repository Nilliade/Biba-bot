"""Оркестратор игры: таймеры, голос, сообщения и переходы между фазами."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

import discord

from biba.database import GuildSettings, HistoryRepo
from biba.game.embeds import (
    aborted_embed,
    betting_embed,
    cancel_embed,
    final_embed,
    host_panel_embed,
    host_pick_embed,
    leaderboard_embed,
    listening_embed,
    lobby_embed,
    mention,
    round_result_embed,
    wrong_guess_embed,
)
from biba.game.engine import MelodyEngine, random_candidate
from biba.game.models import GameError, GameState, Phase, RoundResult
from biba.game.views import (
    BettingView,
    FinalView,
    HostPanelView,
    HostPickView,
    ListeningView,
    LobbyView,
    ResultsView,
)
from biba.music.player import VoiceManager
from biba.music.resolver import TrackResolver

if TYPE_CHECKING:  # pragma: no cover
    from biba.bot import BibaBot

log = logging.getLogger(__name__)

KEEP_VIEW: Any = object()
"""Маркер: не трогать кнопки сообщения при обновлении эмбеда."""


class GameController:
    """Живой экземпляр игры на одном сервере.

    Движок отвечает за правила и деньги, контроллер — за то, *когда*
    показывать экраны, запускать звук и останавливать таймеры.
    """

    def __init__(
        self,
        *,
        bot: BibaBot,
        engine: MelodyEngine,
        resolver: TrackResolver,
        voice: VoiceManager,
        history: HistoryRepo,
        guild: discord.Guild,
        game: GameState,
        settings: GuildSettings,
    ) -> None:
        self.bot = bot
        self.engine = engine
        self.resolver = resolver
        self.voice = voice
        self.history = history
        self.guild = guild
        self.game = game
        self.settings = settings

        self.message: discord.Message | None = None
        self.closed = False
        self.tasks: set[asyncio.Task] = set()

        self._lock = asyncio.Lock()
        self._edit_lock = asyncio.Lock()
        self._prepare_task: asyncio.Task | None = None
        self._auto_start_task: asyncio.Task | None = None
        self._stream_url: str | None = None
        self._betting_deadline = 0.0
        self._host_role_user: int | None = None
        self._finished = False
        self._cleanup_task: asyncio.Task | None = None
        self.pending_track_id: str | None = None
        self.pending_snippet: int | None = None

    # -- базовые помощники ----------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _cancel_tasks(self) -> None:
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()
        self.tasks.clear()

    async def _ephemeral(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"ephemeral": True}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        try:
            if interaction.response.is_done():
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)
        except discord.HTTPException as exc:
            log.warning("Ephemeral response failed: %s", exc)

    async def _edit(
        self,
        embed: discord.Embed,
        view: discord.ui.View | None | Any = KEEP_VIEW,
        *,
        phase: Phase | None = None,
    ) -> None:
        async with self._edit_lock:
            if self.closed:
                return
            if phase is not None and self.game.phase != phase:
                return
            if self.message is None:
                return
            kwargs: dict[str, Any] = {"embed": embed}
            if view is not KEEP_VIEW:
                kwargs["view"] = view
            try:
                await self.message.edit(**kwargs)
            except discord.NotFound:
                self._message_lost()
            except discord.HTTPException:
                log.exception("Failed to edit game message in guild %s", self.game.guild_id)

    def _message_lost(self) -> None:
        log.warning("Game message lost in guild %s; cancelling game", self.game.guild_id)
        self.closed = True
        if self.game.phase != Phase.FINISHED:
            self.engine.cancel(self.game.guild_id, "Сообщение игры было удалено")
        self.engine.remove_game(self.game.guild_id)
        self._cancel_tasks()
        self._cleanup_task = asyncio.create_task(self._cleanup_after_message_loss())

    async def _cleanup_after_message_loss(self) -> None:
        await self.voice.stop(self.game.guild_id)
        await self.voice.disconnect(self.game.guild_id)
        await self._clear_host_role()

    async def _rerender(self) -> None:
        """Заново рисует экран текущей фазы, не меняя логику раунда."""
        game = self.game
        if self.closed:
            return
        if game.phase == Phase.LOBBY:
            await self.refresh_lobby()
            return
        if game.phase == Phase.FINISHED:
            if game.cancelled:
                await self._render_cancel(game.cancel_reason)
            else:
                await self.finish()
            return
        if game.round is not None and game.round.aborted:
            await self._show_aborted()
            return
        if game.phase == Phase.HOST_PICK:
            await self._edit(host_pick_embed(game), HostPickView(self))
        elif game.phase == Phase.BETTING:
            await self._edit(betting_embed(game, self._betting_remaining()))
        elif game.phase == Phase.LISTENING:
            remaining = (
                max(0.0, game.round.deadline - time.monotonic()) if game.round is not None else 0.0
            )
            await self._edit(listening_embed(game, remaining), phase=Phase.LISTENING)
        elif game.phase == Phase.RESULTS:
            await self._edit(
                round_result_embed(game, self.engine.build_round_result(game.guild_id)),
                ResultsView(self),
            )

    # -- лобби -----------------------------------------------------------
    async def send_lobby(self, interaction: discord.Interaction) -> None:
        embed = lobby_embed(self.game, self.settings)
        view = LobbyView(self)

        # Настроенный текстовый канал имеет приоритет, если это другой канал.
        target = None
        if self.settings.text_channel_id:
            target = self.guild.get_channel(self.settings.text_channel_id)
        if target is not None and target.id != interaction.channel_id:
            me = self.guild.me
            allowed = me is not None and target.permissions_for(me).send_messages
            if not allowed:
                target = None
        if target is not None:
            try:
                self.message = await target.send(embed=embed, view=view)
                if interaction.response.is_done():
                    await interaction.followup.send(
                        f"🎮 Игра создана в {target.mention}.", ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        f"🎮 Игра создана в {target.mention}.", ephemeral=True
                    )
            except discord.HTTPException:
                log.exception("Failed to send lobby message to configured channel")
                self.message = None

        if self.message is None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, view=view)
                    self.message = await interaction.original_response()
                else:
                    await interaction.response.send_message(embed=embed, view=view)
                    self.message = await interaction.original_response()
            except discord.HTTPException:
                log.exception("Failed to send lobby message")
                return
        self.game.message_id = self.message.id if self.message else None

    async def refresh_lobby(self) -> None:
        if self.game.phase != Phase.LOBBY:
            return
        await self._edit(lobby_embed(self.game, self.settings), LobbyView(self))

    async def join(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        voice_state = getattr(member, "voice", None)
        if (
            voice_state is None
            or voice_state.channel is None
            or voice_state.channel.id != self.game.voice_channel_id
        ):
            channel = self.guild.get_channel(self.game.voice_channel_id)
            channel_text = channel.mention if channel is not None else "игры"
            await self._ephemeral(
                interaction,
                f"🎧 Сначала зайдите в голосовой канал {channel_text}.",
            )
            return
        try:
            player = self.engine.join(self.game.guild_id, member.id)
        except GameError as exc:
            await self._ephemeral(interaction, str(exc))
            return
        await self._ephemeral(
            interaction,
            f"✅ Вы в игре. Баланс: **{player.balance:,}**.".replace(",", " "),
        )
        await self.refresh_lobby()

    async def leave(self, interaction: discord.Interaction) -> None:
        try:
            self.engine.leave(self.game.guild_id, interaction.user.id)
        except GameError as exc:
            await self._ephemeral(interaction, str(exc))
            return
        await self._ephemeral(interaction, "🚪 Вы покинули игру.")

        if self.engine.get_game(self.game.guild_id) is None:
            await self._edit(cancel_embed("Лобби закрыто: все игроки вышли."), None)
            self.closed = True
            self._finished = True
            return
        await self._rerender()

    async def start_game(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            if interaction.user.id != self.game.host_id:
                await self._ephemeral(interaction, "Только ведущий может начать игру.")
                return
            missing = await self._players_not_in_voice()
            if missing:
                names = ", ".join(mention(user_id) for user_id in missing)
                await self._ephemeral(
                    interaction,
                    "В голосовом канале должны быть все игроки: " + names,
                )
                return
            try:
                round_ = self.engine.start(self.game.guild_id)
            except GameError as exc:
                await self._ephemeral(interaction, str(exc))
                return
            self.pending_track_id = None
            self.pending_snippet = None
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            await self._apply_host_role(round_.host_id)
            await self._edit(host_pick_embed(self.game), HostPickView(self))
            await self._send_host_panel(interaction, round_)
            self._schedule_host_timeout()

    async def _players_not_in_voice(self) -> list[int]:
        missing: list[int] = []
        for user_id in self.game.players:
            member = self.guild.get_member(user_id)
            if member is None:
                try:
                    member = await self.guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            voice = getattr(member, "voice", None) if member is not None else None
            if (
                voice is None
                or voice.channel is None
                or voice.channel.id != self.game.voice_channel_id
            ):
                missing.append(user_id)
        return missing

    async def _send_host_panel(self, interaction: discord.Interaction, round_: Any) -> None:
        try:
            await interaction.followup.send(
                embed=host_panel_embed(self.game),
                view=HostPanelView(self, round_),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.warning("Failed to send host panel: %s", exc)
            await self._send_host_panel_dm(round_)

    async def reopen_host_panel(self, interaction: discord.Interaction) -> None:
        if self.game.phase != Phase.HOST_PICK or self.game.round is None:
            await self._ephemeral(interaction, "Сейчас ведущий не выбирает мелодию.")
            return
        if interaction.user.id != self.game.round.host_id:
            await self._ephemeral(interaction, "Панель ведущего доступна только текущему ведущему.")
            return
        await self._ephemeral(
            interaction,
            embed=host_panel_embed(self.game),
            view=HostPanelView(self, self.game.round),
        )

    async def reroll(self, interaction: discord.Interaction) -> None:
        async with self._lock:
            try:
                self.engine.reroll(self.game.guild_id, interaction.user.id)
            except GameError as exc:
                await self._ephemeral(interaction, str(exc))
                return
            self.pending_track_id = None
            await self._edit(host_pick_embed(self.game), HostPickView(self))
            try:
                await interaction.response.edit_message(
                    embed=host_panel_embed(self.game),
                    view=HostPanelView(self, self.game.round),
                )
            except discord.HTTPException as exc:
                log.warning("Failed to update host panel after reroll: %s", exc)

    # -- раунд: ставки ---------------------------------------------------
    async def begin_betting(
        self,
        interaction: discord.Interaction | None,
        track_id: str | None,
        snippet: int | None,
    ) -> None:
        if self.closed:
            return
        async with self._lock:
            game = self.game
            if game.phase != Phase.HOST_PICK:
                if interaction is not None:
                    await self._ephemeral(interaction, "Раунд уже запущен.")
                return
            host_id = game.round.host_id if game.round is not None else game.host_id
            try:
                if track_id:
                    self.engine.choose_track(game.guild_id, host_id, track_id)
                if snippet:
                    self.engine.set_snippet(game.guild_id, host_id, snippet)
                round_ = self.engine.begin_betting(game.guild_id, host_id)
            except GameError as exc:
                if interaction is not None:
                    await self._ephemeral(interaction, str(exc))
                return
            if interaction is not None:
                try:
                    await interaction.response.edit_message(
                        content="✅ Раунд запущен. Ставки открыты.",
                        embed=None,
                        view=None,
                    )
                except discord.HTTPException:
                    pass
            self._stream_url = None
            self._betting_deadline = time.monotonic() + game.betting_seconds
            self.pending_track_id = None
            self.pending_snippet = None
            await self._edit(betting_embed(game, game.betting_seconds), BettingView(self))
            self._prepare_task = self._spawn(self._prepare_round(round_))
            self._spawn(self._betting_timer())
            self._maybe_auto_start()

    async def _prepare_round(self, round_: Any) -> None:
        track = round_.track
        if track is None:
            self._stream_url = None
            return
        try:
            self._stream_url = await self.resolver.stream_url(track)
        except Exception as exc:  # noqa: BLE001
            log.warning("Stream resolve failed for %s: %s", track.id, exc)
            self._stream_url = None
        if self.voice.is_enabled:
            channel = self.guild.get_channel(self.game.voice_channel_id)
            await self.voice.connect(self.guild, channel)

    async def _betting_timer(self) -> None:
        await asyncio.sleep(max(1, self.game.betting_seconds))
        if self.game.phase == Phase.BETTING:
            await self._start_listening(None)

    def _maybe_auto_start(self) -> None:
        if self._auto_start_task is not None and not self._auto_start_task.done():
            return
        if not self.engine.all_bets_placed(self.game):
            return
        self._auto_start_task = self._spawn(self._auto_start_after(3.0))

    async def _auto_start_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.game.phase == Phase.BETTING and self.engine.all_bets_placed(self.game):
            await self._start_listening(None)

    def _betting_remaining(self) -> float:
        if not self._betting_deadline:
            return float(self.game.betting_seconds)
        return max(0.0, self._betting_deadline - time.monotonic())

    async def handle_bet(self, interaction: discord.Interaction, amount: Any) -> None:
        if self.closed:
            await self._ephemeral(interaction, "Игра уже завершена.")
            return
        async with self._lock:
            game = self.game
            if game.phase != Phase.BETTING:
                await self._ephemeral(interaction, "Ставки уже закрыты.")
                return
            if isinstance(amount, str) and amount.strip().lower() in {"all", "all-in", "ва-банк"}:
                player = game.players.get(interaction.user.id)
                if player is None:
                    await self._ephemeral(interaction, "Вы не участвуете в этой игре.")
                    return
                amount = player.balance + player.bet
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                await self._ephemeral(interaction, "Введите целое число баллов.")
                return
            try:
                bet = self.engine.place_bet(game.guild_id, interaction.user.id, amount)
            except GameError as exc:
                await self._ephemeral(interaction, str(exc))
                return
            player = game.players.get(interaction.user.id)
            balance = player.balance if player is not None else 0
            await self._ephemeral(
                interaction,
                f"✅ Ставка **{bet:,}** принята. Баланс: **{balance:,}**.".replace(",", " "),
            )
            await self._edit(betting_embed(game, self._betting_remaining()))
            self._maybe_auto_start()

    # -- раунд: прослушивание -------------------------------------------
    async def start_listening_from_interaction(self, interaction: discord.Interaction) -> None:
        if self.game.round is not None and interaction.user.id != self.game.round.host_id:
            await self._ephemeral(interaction, "Только ведущий может запустить прослушивание.")
            return
        await self._start_listening(interaction)

    async def _start_listening(self, interaction: discord.Interaction | None) -> None:
        if self.closed:
            return
        async with self._lock:
            game = self.game
            if game.phase != Phase.BETTING:
                if interaction is not None:
                    await self._ephemeral(interaction, "Ставки уже закрыты.")
                return
            if game.round is None or not game.round.bets:
                if interaction is not None:
                    await self._ephemeral(interaction, "Никто не сделал ставку — раунд не запущен.")
                else:
                    self.engine.abort_round(game.guild_id, "Никто не сделал ставку")
                    await self._edit(
                        aborted_embed(game, "Никто не сделал ставку."), ResultsView(self)
                    )
                    self._spawn(self._next_round_prompt())
                return
            if interaction is not None:
                try:
                    await interaction.response.defer()
                except discord.HTTPException:
                    pass

            if self._prepare_task is not None:
                try:
                    await self._prepare_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass

            if self.closed or self._finished:
                return
            if not self._stream_url:
                self._abort_current_round("Не удалось получить аудиопоток. Ставки возвращены.")
                return
            if self.voice.is_enabled:
                channel = self.guild.get_channel(game.voice_channel_id)
                connected = await self.voice.connect(self.guild, channel)
                if not connected:
                    self._abort_current_round(
                        "Не удалось подключиться к голосовому каналу. Ставки возвращены."
                    )
                    return
                offset = self._offset_for(game.round.track, game.round.snippet_sec)
                volume = 70
                db = getattr(self.bot, "db", None)
                if db is not None:
                    try:
                        volume = db.get_volume(game.guild_id)
                    except Exception:  # noqa: BLE001
                        volume = 70
                played = await self.voice.play(
                    self.guild, self._stream_url, offset=offset, volume=volume
                )
                if not played:
                    self._abort_current_round("Не удалось воспроизвести трек. Ставки возвращены.")
                    return

            try:
                self.engine.start_listening(game.guild_id)
            except GameError as exc:
                await self.voice.stop(game.guild_id)
                self._abort_current_round(f"{exc} Ставки возвращены.")
                return
            await self._edit(listening_embed(game, game.round.snippet_sec), ListeningView(self))
            self._spawn(self._listening_loop())

    def _offset_for(self, track: Any, duration: int) -> int:
        if not self.game.random_offset or track is None:
            return 0
        if not track.duration or track.duration <= duration + 10:
            return 0
        return random.randint(0, max(0, track.duration - duration - 5))

    def _abort_current_round(self, reason: str) -> None:
        if self.closed or self._finished:
            return
        self.engine.abort_round(self.game.guild_id, reason)
        self._spawn(self._render_aborted(reason))

    async def _render_aborted(self, reason: str) -> None:
        await self.voice.stop(self.game.guild_id)
        await self._edit(aborted_embed(self.game, reason), ResultsView(self))
        self._spawn(self._next_round_prompt())

    async def _listening_loop(self) -> None:
        game = self.game
        while game.phase == Phase.LISTENING and game.round is not None:
            remaining = game.round.deadline - time.monotonic()
            if remaining <= 0:
                break
            await self._edit(
                listening_embed(game, remaining),
                phase=Phase.LISTENING,
            )
            await asyncio.sleep(min(2.0, max(0.25, remaining)))
        if game.phase != Phase.LISTENING:
            return
        try:
            result = self.engine.close_round(game.guild_id)
        except GameError:
            return
        await self._after_round(result)

    async def handle_guess(self, interaction: discord.Interaction, text: str) -> None:
        if self.closed:
            await self._ephemeral(interaction, "Игра уже завершена.")
            return
        game = self.game
        if game.phase != Phase.LISTENING:
            await self._ephemeral(interaction, "Сейчас нельзя угадывать.")
            return
        try:
            outcome = self.engine.submit_guess(game.guild_id, interaction.user.id, text)
        except GameError as exc:
            await self._ephemeral(interaction, str(exc))
            return
        if not outcome.correct:
            await self._ephemeral(interaction, embed=wrong_guess_embed(outcome))
            return
        await self._ephemeral(
            interaction,
            f"🎉 Верно! Выигрыш: **{outcome.payout:,}** баллов.".replace(",", " "),
        )
        await self.voice.stop(game.guild_id)
        result = self.engine.build_round_result(game.guild_id)
        await self._after_round(result)

    async def _after_round(self, result: RoundResult) -> None:
        game = self.game
        await self.voice.stop(game.guild_id)
        round_ = game.round
        if round_ is not None:
            self._record_history(round_)
        await self._edit(round_result_embed(game, result), ResultsView(self))
        self._spawn(self._next_round_prompt())

    def _record_history(self, round_: Any) -> None:
        track = round_.track
        try:
            self.history.record_round(
                guild_id=self.game.guild_id,
                round_number=round_.number,
                host_id=round_.host_id,
                track_title=track.title if track else None,
                track_artist=track.artist if track else None,
                snippet_sec=round_.snippet_sec,
                winner_id=round_.winner_id,
                winner_elapsed=round_.winner_elapsed if round_.winner_id is not None else None,
                winner_multiplier=round_.winner_multiplier
                if round_.winner_id is not None
                else None,
                winner_payout=round_.winner_payout,
                total_bets=round_.total_bets,
                host_fee=round_.host_fee,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to record round history")

    async def _next_round_prompt(self) -> None:
        game = self.game
        if game.round_number >= game.rounds_total:
            await asyncio.sleep(15)
            if game.phase == Phase.RESULTS:
                await self.finish()
            return
        await asyncio.sleep(90)
        if game.phase == Phase.RESULTS:
            await self.next_round(None, auto=True)

    # -- следующие раунды и финал ---------------------------------------
    async def next_round(
        self,
        interaction: discord.Interaction | None,
        *,
        auto: bool = False,
    ) -> None:
        game = self.game
        if self.closed:
            if interaction is not None:
                await self._ephemeral(interaction, "Игра уже завершена.")
            return
        async with self._lock:
            if game.phase != Phase.RESULTS:
                if interaction is not None:
                    await self._ephemeral(interaction, "Раунд ещё не завершён.")
                return
            next_host = game.next_host_id
            if (
                interaction is not None
                and next_host is not None
                and interaction.user.id != next_host
            ):
                await self._ephemeral(
                    interaction,
                    f"Следующий ведущий — {mention(next_host)}. Дождитесь его хода "
                    "или используйте /melody next.",
                )
                return
            try:
                round_ = self.engine.next_round(game.guild_id)
            except GameError as exc:
                if interaction is not None:
                    await self._ephemeral(interaction, str(exc))
                return

        if round_ is None:
            if interaction is not None and not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except discord.HTTPException:
                    pass
            await self.finish()
            return

        self.pending_track_id = None
        self.pending_snippet = None
        await self._apply_host_role(round_.host_id)
        if auto:
            track = random_candidate(round_)
            if track is not None:
                await self.begin_betting(None, track.id, round_.snippet_sec)
            return

        await self._edit(host_pick_embed(game), HostPickView(self))
        if interaction is not None:
            await self._ephemeral(
                interaction,
                embed=host_panel_embed(game),
                view=HostPanelView(self, round_),
            )
        else:
            await self._send_host_panel_dm(round_)
        self._schedule_host_timeout()

    async def _send_host_panel_dm(self, round_: Any) -> None:
        member = self.guild.get_member(round_.host_id)
        if member is None:
            try:
                member = await self.guild.fetch_member(round_.host_id)
            except discord.HTTPException:
                member = None
        if member is not None:
            try:
                await member.send(
                    embed=host_panel_embed(self.game),
                    view=HostPanelView(self, round_),
                )
                return
            except discord.HTTPException:
                log.info(
                    "Cannot DM host %s in guild %s; using public fallback",
                    round_.host_id,
                    self.game.guild_id,
                )
        if self.message is not None:
            try:
                await self.message.channel.send(
                    f"🎙️ {mention(round_.host_id)}, ваш ход! Нажмите «Панель ведущего».",
                    view=HostPickView(self),
                )
            except discord.HTTPException:
                pass

    def _schedule_host_timeout(self) -> None:
        self._spawn(self._host_timeout())

    async def _host_timeout(self) -> None:
        await asyncio.sleep(150)
        await self._auto_pick_track()

    async def _auto_pick_track(self) -> None:
        game = self.game
        if game.phase != Phase.HOST_PICK or game.round is None:
            return
        track = None
        if self.pending_track_id:
            track = next(
                (
                    candidate
                    for candidate in game.round.candidates
                    if candidate.id == self.pending_track_id
                ),
                None,
            )
        if track is None:
            track = random_candidate(game.round)
        snippet = self.pending_snippet or game.round.snippet_sec
        if track is not None:
            await self.begin_betting(None, track.id, snippet)

    # -- завершение ------------------------------------------------------
    async def finish(self) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            game = self.game
            if game.phase != Phase.FINISHED:
                self.engine.finish_game(game.guild_id)
            await self.voice.stop(game.guild_id)
            await self.voice.disconnect(game.guild_id)
            await self._clear_host_role()
            await self._edit(final_embed(game, game.leaderboard()), FinalView(self))
            self.engine.remove_game(game.guild_id)
            self.closed = True
            self._cancel_tasks()

    async def finish_from_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.host_id and not getattr(
            getattr(interaction.user, "guild_permissions", None),
            "manage_guild",
            False,
        ):
            await self._ephemeral(
                interaction, "Завершить игру может только ведущий или администратор."
            )
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self.finish()

    async def cancel(
        self,
        interaction: discord.Interaction | None,
        reason: str = "Игра отменена",
    ) -> None:
        async with self._lock:
            if self._finished or self.closed:
                return
            self._finished = True
            game = self.game
            if game.phase != Phase.FINISHED:
                self.engine.cancel(game.guild_id, reason)
            await self.voice.stop(game.guild_id)
            await self.voice.disconnect(game.guild_id)
            await self._clear_host_role()
            await self._edit(cancel_embed(reason), None)
            self.engine.remove_game(game.guild_id)
            self.closed = True
            if interaction is not None and not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except discord.HTTPException:
                    pass
            self._cancel_tasks()

    async def cancel_from_interaction(self, interaction: discord.Interaction) -> None:
        allowed = interaction.user.id == self.game.host_id or getattr(
            getattr(interaction.user, "guild_permissions", None), "manage_guild", False
        )
        if not allowed:
            await self._ephemeral(
                interaction, "Остановить игру может только ведущий или администратор."
            )
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self.cancel(interaction, "Ведущий остановил игру")

    async def _render_cancel(self, reason: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        await self.voice.stop(self.game.guild_id)
        await self.voice.disconnect(self.game.guild_id)
        await self._clear_host_role()
        await self._edit(cancel_embed(reason or self.game.cancel_reason), None)
        self.engine.remove_game(self.game.guild_id)
        self.closed = True
        self._cancel_tasks()

    async def _show_aborted(self) -> None:
        round_ = self.game.round
        reason = round_.abort_reason if round_ is not None else ""
        await self.voice.stop(self.game.guild_id)
        await self._edit(aborted_embed(self.game, reason), ResultsView(self))
        self._spawn(self._next_round_prompt())

    async def show_leaderboard(self, interaction: discord.Interaction) -> None:
        await self._ephemeral(
            interaction,
            embed=leaderboard_embed(
                self.game.leaderboard(), self.guild, title="📊 Текущая таблица"
            ),
        )

    async def close_message(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    # -- роль ведущего ---------------------------------------------------
    async def _apply_host_role(self, host_id: int) -> None:
        role_id = self.game.host_role_id
        if not role_id:
            return
        role = self.guild.get_role(role_id)
        if role is None:
            log.warning("Host role %s not found in guild %s", role_id, self.game.guild_id)
            return
        if self._host_role_user is not None and self._host_role_user != host_id:
            await self._remove_role(self._host_role_user, role)
        await self._add_role(host_id, role)
        self._host_role_user = host_id
        self._save_host(host_id, role.id)

    async def _add_role(self, user_id: int, role: discord.Role) -> None:
        member = self.guild.get_member(user_id)
        if member is None:
            try:
                member = await self.guild.fetch_member(user_id)
            except discord.HTTPException:
                return
        try:
            await member.add_roles(role, reason="Угадай мелодию: ведущий раунда")
        except discord.HTTPException as exc:
            log.warning("Failed to add host role to %s: %s", user_id, exc)

    async def _remove_role(self, user_id: int, role: discord.Role) -> None:
        member = self.guild.get_member(user_id)
        if member is None:
            try:
                member = await self.guild.fetch_member(user_id)
            except discord.HTTPException:
                return
        try:
            await member.remove_roles(role, reason="Угадай мелодию: смена ведущего")
        except discord.HTTPException as exc:
            log.warning("Failed to remove host role from %s: %s", user_id, exc)

    async def _clear_host_role(self) -> None:
        if self._host_role_user is None:
            self._forget_host()
            return
        role_id = self.game.host_role_id
        role = self.guild.get_role(role_id) if role_id else None
        if role is not None:
            await self._remove_role(self._host_role_user, role)
        self._host_role_user = None
        self._forget_host()

    def _save_host(self, user_id: int, role_id: int) -> None:
        hosts = getattr(self.bot, "hosts", None)
        if hosts is None:
            return
        try:
            hosts.set(self.game.guild_id, user_id, role_id)
        except Exception:  # noqa: BLE001
            log.exception("Failed to store host role state")

    def _forget_host(self) -> None:
        hosts = getattr(self.bot, "hosts", None)
        if hosts is None:
            return
        try:
            hosts.clear(self.game.guild_id)
        except Exception:  # noqa: BLE001
            log.exception("Failed to clear host role state")
