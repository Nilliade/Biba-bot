from __future__ import annotations

from types import SimpleNamespace

from biba.game.models import Phase
from biba.game.views import (
    BetModal,
    BettingView,
    FinalView,
    GuessModal,
    HostPanelView,
    HostPickView,
    ListeningView,
    LobbyView,
    ResultsView,
)
from tests.test_embeds import make_game


def controller_for(game):
    return SimpleNamespace(game=game)


def test_views_construct() -> None:
    game = make_game()
    controller = controller_for(game)
    for view_cls in (LobbyView, HostPickView, BettingView, ListeningView, ResultsView, FinalView):
        view = view_cls(controller)
        assert isinstance(view.children, list)
    assert BetModal(controller).title == "Своя ставка"
    assert GuessModal(controller).title == "Угадай мелодию"


def test_host_panel_options_and_rebuild() -> None:
    game = make_game()
    game.phase = Phase.HOST_PICK
    controller = controller_for(game)
    view = HostPanelView(controller, game.round)
    assert len(view.track_select.options) == len(game.round.candidates)
    assert view.start_button.disabled

    view.selected_id = game.round.candidates[0].id
    view.snippet = 20
    rebuilt = view._rebuild()
    assert rebuilt.start_button.disabled is False
    assert rebuilt.snippet == 20
    defaults = [option.default for option in rebuilt.duration_select.options]
    assert defaults[3] is True  # 20 секунд
