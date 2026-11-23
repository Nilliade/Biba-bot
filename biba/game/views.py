"""Discord-компоненты игры: кнопки, селекты и модалки."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from biba.game.embeds import host_panel_embed

if TYPE_CHECKING:  # pragma: no cover - только для типов
    from biba.game.controller import GameController
    from biba.game.models import RoundState


class LobbyView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.join(interaction)

    @discord.ui.button(label="Покинуть", style=discord.ButtonStyle.secondary, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.leave(interaction)

    @discord.ui.button(label="Начать игру", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.start_game(interaction)

    @discord.ui.button(label="Отменить", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.cancel_from_interaction(interaction)


class HostPickView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Панель ведущего", style=discord.ButtonStyle.primary, emoji="🎛️")
    async def panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.reopen_host_panel(interaction)

    @discord.ui.button(label="Отменить игру", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.cancel_from_interaction(interaction)


class BettingView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="10", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def bet_10(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.handle_bet(interaction, 10)

    @discord.ui.button(label="50", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def bet_50(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.handle_bet(interaction, 50)

    @discord.ui.button(label="100", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def bet_100(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.handle_bet(interaction, 100)

    @discord.ui.button(label="500", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def bet_500(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.handle_bet(interaction, 500)

    @discord.ui.button(label="ALL-IN", style=discord.ButtonStyle.danger, emoji="🔥")
    async def bet_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.handle_bet(interaction, "all")

    @discord.ui.button(label="Своя ставка", style=discord.ButtonStyle.primary, emoji="✏️")
    async def bet_custom(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(BetModal(self.controller))

    @discord.ui.button(
        label="▶ Начать прослушивание", style=discord.ButtonStyle.success, emoji="🎧"
    )
    async def start_listening(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.start_listening_from_interaction(interaction)


class ListeningView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Угадать мелодию", style=discord.ButtonStyle.success, emoji="🎯")
    async def guess(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(GuessModal(self.controller))


class ResultsView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Следующий раунд", style=discord.ButtonStyle.success, emoji="▶️")
    async def next_round(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.next_round(interaction)

    @discord.ui.button(label="Таблица", style=discord.ButtonStyle.secondary, emoji="📊")
    async def table(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.show_leaderboard(interaction)

    @discord.ui.button(label="Завершить игру", style=discord.ButtonStyle.danger, emoji="🛑")
    async def finish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.finish_from_interaction(interaction)


class FinalView(discord.ui.View):
    def __init__(self, controller: GameController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Таблица", style=discord.ButtonStyle.secondary, emoji="📊")
    async def table(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.show_leaderboard(interaction)

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.controller.close_message(interaction)


class BetModal(discord.ui.Modal, title="Своя ставка"):
    amount = discord.ui.TextInput(
        label="Сумма ставки",
        placeholder="Например: 250",
        max_length=9,
        required=True,
    )

    def __init__(self, controller: GameController) -> None:
        super().__init__()
        self.controller = controller

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount.value).strip().replace(" ", "")
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message("Введите целое число баллов.", ephemeral=True)
            return
        await self.controller.handle_bet(interaction, value)


class GuessModal(discord.ui.Modal, title="Угадай мелодию"):
    answer = discord.ui.TextInput(
        label="Название или исполнитель",
        placeholder="Например: Queen — Bohemian Rhapsody",
        max_length=100,
        required=True,
    )

    def __init__(self, controller: GameController) -> None:
        super().__init__()
        self.controller = controller

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.controller.handle_guess(interaction, str(self.answer.value))


class HostPanelView(discord.ui.View):
    """Эфемерная панель ведущего: выбор трека и длительности."""

    def __init__(
        self,
        controller: GameController,
        round_: RoundState,
        selected_id: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.controller = controller
        self.round = round_
        self.selected_id = selected_id
        self.snippet = round_.snippet_sec

        options = []
        for index, track in enumerate(round_.candidates, start=1):
            label = f"{index}. {track.label}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=track.id,
                    description=f"{track.duration_label} · {track.artist or '—'}"[:100],
                    default=track.id == selected_id,
                    emoji="🎵",
                )
            )
        self.track_select = discord.ui.Select(
            placeholder="Выберите мелодию...",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Нет треков", value="none")],
            row=0,
        )
        self.track_select.callback = self._on_track_select
        self.add_item(self.track_select)

        self.duration_select = discord.ui.Select(
            placeholder="Длительность превью",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{seconds} секунд",
                    value=str(seconds),
                    default=seconds == self.snippet,
                    emoji="⏱",
                )
                for seconds in (5, 10, 15, 20, 30)
            ],
            row=1,
        )
        self.duration_select.callback = self._on_duration_select
        self.add_item(self.duration_select)

        self.start_button = discord.ui.Button(
            label="▶ Запустить раунд",
            style=discord.ButtonStyle.success,
            emoji="🎧",
            row=2,
            disabled=selected_id is None,
        )
        self.start_button.callback = self._on_start
        self.add_item(self.start_button)

        self.reroll_button = discord.ui.Button(
            label="🔄 Обновить варианты",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=controller.game.rerolls_left <= 0,
        )
        self.reroll_button.callback = self._on_reroll
        self.add_item(self.reroll_button)

        self.cancel_button = discord.ui.Button(
            label="🛑 Отменить",
            style=discord.ButtonStyle.danger,
            row=2,
        )
        self.cancel_button.callback = self._on_cancel
        self.add_item(self.cancel_button)

    def _rebuild(self) -> HostPanelView:
        view = HostPanelView(self.controller, self.round, self.selected_id)
        view.snippet = self.snippet
        # Пересобираем селект длительности с новым значением по умолчанию.
        view.duration_select.options = [
            discord.SelectOption(
                label=f"{seconds} секунд",
                value=str(seconds),
                default=seconds == self.snippet,
                emoji="⏱",
            )
            for seconds in (5, 10, 15, 20, 30)
        ]
        view.start_button.disabled = self.selected_id is None
        return view

    async def _edit_panel(self, interaction: discord.Interaction) -> None:
        view = self._rebuild()
        await interaction.response.edit_message(
            embed=host_panel_embed(self.controller.game, self.selected_id),
            view=view,
        )

    async def _on_track_select(self, interaction: discord.Interaction) -> None:
        self.selected_id = self.track_select.values[0]
        self.controller.pending_track_id = self.selected_id
        await self._edit_panel(interaction)

    async def _on_duration_select(self, interaction: discord.Interaction) -> None:
        self.snippet = int(self.duration_select.values[0])
        self.round.snippet_sec = self.snippet
        self.controller.pending_snippet = self.snippet
        await self._edit_panel(interaction)

    async def _on_start(self, interaction: discord.Interaction) -> None:
        await self.controller.begin_betting(interaction, self.selected_id, self.snippet)

    async def _on_reroll(self, interaction: discord.Interaction) -> None:
        await self.controller.reroll(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self.controller.cancel_from_interaction(interaction)
