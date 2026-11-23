"""Discord-эмбеды для игры. Чистые функции — удобно тестировать."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import discord

from biba.database import GuildSettings
from biba.game.models import GameState, GuessOutcome, RoundResult
from biba.game.scoring import multiplier_tier_label, streak_multiplier
from biba.models import Track

COLOR_MAIN = 0x8B5CF6
COLOR_SUCCESS = 0x22C55E
COLOR_DANGER = 0xEF4444
COLOR_GOLD = 0xF59E0B
COLOR_INFO = 0x3B82F6
COLOR_ORANGE = 0xF97316


def mention(user_id: int | None) -> str:
    return f"<@{user_id}>" if user_id else "—"


def channel_mention(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "—"


def role_mention(role_id: int | None) -> str:
    return f"<@&{role_id}>" if role_id else "—"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 100 in (11, 14):
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def fmt_points(n: int) -> str:
    return f"**{int(n):,}**".replace(",", " ") + " " + plural(int(n), "балл", "балла", "баллов")


def fmt_multiplier(value: float) -> str:
    text = f"×{float(value):.2f}".rstrip("0").rstrip(".")
    return text


def progress_bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "▰" * filled + "▱" * (width - filled)


def _player_names(game: GameState) -> str:
    if not game.players:
        return "Пока никого"
    text = ", ".join(mention(user_id) for user_id in game.players)
    return text if len(text) <= 1000 else text[:997] + "..."


def _bets_text(game: GameState, *, reveal: bool = True) -> str:
    round_ = game.round
    if round_ is None or not round_.bets:
        return "Пока никто не поставил"
    lines = []
    for user_id, amount in round_.bets.items():
        player = game.players.get(user_id)
        suffix = ""
        if player and player.bet >= player.balance + player.bet and player.bet > 0:
            suffix = " (all-in)"
        lines.append(f"{mention(user_id)} — **{amount:,}**{suffix}".replace(",", " "))
    text = "\n".join(lines)
    return text if len(text) <= 1000 else text[:997] + "..."


def _leaderboard_rows(rows: Iterable[Any], guild: discord.Guild | None = None) -> str:
    medals = ("🥇", "🥈", "🥉")
    lines: list[str] = []
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            user_id = int(row["user_id"])
            balance = int(row["balance"])
            wins = int(row.get("wins") or 0)
            streak = int(row.get("current_streak") or 0)
        else:
            user_id, balance, streak, wins = row
        medal = medals[index] if index < len(medals) else f"{index + 1}."
        name = mention(user_id)
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                name = member.display_name
        extra = f" · {wins} {plural(wins, 'победа', 'победы', 'побед')}" if wins else ""
        if streak > 1:
            extra += f" · 🔥 {streak}"
        lines.append(f"{medal} {name} — **{balance:,}**{extra}".replace(",", " "))
        if len(lines) >= 12:
            break
    return "\n".join(lines) if lines else "Пока никого"


def lobby_embed(game: GameState, settings: GuildSettings) -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Угадай мелодию",
        description=(
            "Ведущий загадывает трек, игроки делают ставки и угадывают мелодию на скорость. "
            "Чем быстрее ответ — тем выше множитель."
        ),
        color=COLOR_MAIN,
    )
    embed.add_field(
        name="👥 Игроки",
        value=_player_names(game),
        inline=False,
    )
    embed.add_field(
        name="🎯 Формат",
        value=(
            f"{game.rounds_total} {plural(game.rounds_total, 'раунд', 'раунда', 'раундов')}\n"
            f"Превью: {game.default_snippet_sec} с\n"
            f"Ставка от {game.min_bet}"
        ),
        inline=True,
    )
    embed.add_field(
        name="💰 Баллы",
        value=f"Старт: {game.starting_balance:,}\nКомиссия ведущего: {game.host_fee_percent}%".replace(
            ",", " "
        ),
        inline=True,
    )
    embed.add_field(name="🎙 Ведущий", value=mention(game.host_id), inline=True)
    embed.set_footer(text="Нажмите «Присоединиться», чтобы играть. Минимум 2 игрока.")
    return embed


def host_pick_embed(game: GameState) -> discord.Embed:
    round_ = game.round
    embed = discord.Embed(
        title="🎙️ Ведущий выбирает мелодию",
        description=(
            f"{mention(round_.host_id if round_ else game.host_id)}, панель выбора отправлена вам. "
            f"Раунд {game.round_number}/{game.rounds_total}."
        ),
        color=COLOR_INFO,
    )
    embed.add_field(name="👥 Игроки", value=_player_names(game), inline=False)
    embed.add_field(
        name="⏱ Длительность превью",
        value=f"{round_.snippet_sec if round_ else game.default_snippet_sec} с",
        inline=True,
    )
    embed.add_field(
        name="🎲 Вариантов", value=str(len(round_.candidates) if round_ else 0), inline=True
    )
    embed.add_field(
        name="💡 Что дальше",
        value="Ведущий выбирает трек и длительность, затем открываются ставки.",
        inline=False,
    )
    embed.set_footer(text="Панель ведущего видна только ему.")
    return embed


def betting_embed(game: GameState, remaining: float | None = None) -> discord.Embed:
    round_ = game.round
    window = float(game.betting_seconds)
    remaining = window if remaining is None else max(0.0, float(remaining))
    fraction = 1.0 - (remaining / window if window else 0.0)
    embed = discord.Embed(
        title="💰 Сделайте ставки",
        description=(
            "Мелодия загадана. Поставьте баллы на то, что угадаете её быстрее всех. "
            "Кто не угадает — теряет ставку."
        ),
        color=COLOR_GOLD,
    )
    embed.add_field(
        name="⏱ Время на ставки",
        value=f"{progress_bar(fraction)}\nОсталось: **{remaining:.0f} с**",
        inline=False,
    )
    embed.add_field(
        name="🎯 Множители",
        value="×3 — первые 25% времени\n×2 — до 50%\n×1.5 — до 75%\n×1.25 — до конца",
        inline=True,
    )
    embed.add_field(
        name="⏱ Превью",
        value=f"{round_.snippet_sec if round_ else game.default_snippet_sec} с",
        inline=True,
    )
    embed.add_field(name="💵 Ставки", value=_bets_text(game), inline=False)
    embed.add_field(
        name="🎙 Ведущий", value=mention(round_.host_id if round_ else None), inline=True
    )
    embed.set_footer(text="Ошибка в ответе снижает множитель на 0.25.")
    return embed


def listening_embed(game: GameState, remaining: float | None = None) -> discord.Embed:
    round_ = game.round
    if round_ is None:
        return error_embed("Раунд не найден.")
    window = float(round_.snippet_sec)
    remaining = max(0.0, float(remaining)) if remaining is not None else window
    elapsed = max(0.0, window - remaining)
    fraction = 1.0 - (remaining / window if window else 0.0)
    embed = discord.Embed(
        title="🎧 ИГРАЕТ МЕЛОДИЯ",
        description=(
            "Нажмите **«Угадать мелодию»** и введите название или исполнителя. "
            "Каждая ошибка снижает множитель."
        ),
        color=COLOR_SUCCESS,
    )
    embed.add_field(
        name="⏱ Осталось",
        value=f"{progress_bar(fraction)}\n**{remaining:.0f} с** из {window:.0f} с",
        inline=False,
    )
    embed.add_field(
        name="🎯 Множитель сейчас",
        value=f"**{multiplier_tier_label(elapsed, window)}**",
        inline=True,
    )
    embed.add_field(name="💵 Ставки", value=_bets_text(game), inline=False)
    embed.add_field(name="🎙 Ведущий", value=mention(round_.host_id), inline=True)
    embed.add_field(
        name="🏆 Банк раунда",
        value=f"**{round_.total_bets:,}**".replace(",", " "),
        inline=True,
    )
    embed.set_footer(
        text="Первая верная версия забирает банк. Не спамьте — между попытками секунда."
    )
    return embed


def round_result_embed(game: GameState, result: RoundResult | None) -> discord.Embed:
    round_ = game.round
    if round_ is None:
        return error_embed("Раунд не найден.")
    if round_.aborted:
        embed = discord.Embed(
            title="⚠️ Раунд отменён",
            description=round_.abort_reason or "Раунд отменён.",
            color=COLOR_ORANGE,
        )
        embed.add_field(name="💵 Ставки", value="Возвращены игрокам.", inline=False)
        embed.add_field(name="🎙 Ведущий", value=mention(round_.host_id), inline=True)
        return embed

    track = round_.track
    if result is not None and result.winner is not None:
        winner = result.winner
        bet = round_.bets.get(winner.user_id, 0)
        net = max(0, result.payout - bet)
        guess_text = ""
        for record in reversed(round_.guesses):
            if record.correct and record.user_id == winner.user_id:
                guess_text = record.text
                break
        embed = discord.Embed(
            title=f"🏆 Раунд {round_.number}: {mention(winner.user_id)} угадал!",
            description=(
                f"**{track.label if track else '—'}**\n"
                f"⏱ {round_.winner_elapsed:.1f} с · {fmt_multiplier(result.multiplier)} · "
                f"ставка **{bet:,}** → выигрыш **{result.payout:,}** "
                f"(чистыми +{net:,})".replace(",", " ")
            ),
            color=COLOR_GOLD,
        )
        if guess_text:
            embed.add_field(name="💬 Ответ", value=guess_text[:200], inline=True)
        embed.add_field(
            name="🎙 Ведущему",
            value=f"+{result.host_fee:,} ({game.host_fee_percent}% от банка)".replace(",", " "),
            inline=True,
        )
        embed.add_field(
            name="🔥 Серия",
            value=f"{winner.streak} {plural(winner.streak, 'победа', 'победы', 'побед')} подряд\n"
            f"Бонус серии: {fmt_multiplier(streak_multiplier(winner.streak))}",
            inline=True,
        )
        embed.add_field(
            name="📊 Таблица", value=_leaderboard_rows(result.leaderboard), inline=False
        )
        return embed

    embed = discord.Embed(
        title=f"😞 Раунд {round_.number}: никто не угадал",
        description=(
            f"Мелодия: **{track.label if track else '—'}**\n"
            f"⏱ {round_.snippet_sec} с · банк **{result.total_bets if result else round_.total_bets:,}**".replace(
                ",", " "
            )
        ),
        color=COLOR_DANGER,
    )
    embed.add_field(
        name="🎙 Ведущему",
        value=f"+{round_.host_fee:,} (включая бонус за сложный трек)".replace(",", " "),
        inline=True,
    )
    embed.add_field(name="💵 Ставки", value="Сгорели.", inline=True)
    embed.add_field(
        name="📊 Таблица",
        value=_leaderboard_rows(result.leaderboard if result else game.leaderboard()),
        inline=False,
    )
    return embed


def aborted_embed(game: GameState, reason: str) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Раунд отменён",
        description=reason,
        color=COLOR_ORANGE,
    )
    embed.add_field(name="💵 Ставки", value="Возвращены игрокам.", inline=False)
    embed.add_field(
        name="🎙 Ведущий", value=mention(game.round.host_id if game.round else None), inline=True
    )
    return embed


def final_embed(game: GameState, rows: list[Any]) -> discord.Embed:
    winner = game.final_winner_id
    embed = discord.Embed(
        title="🏁 Игра окончена",
        description=(
            f"Победитель — {mention(winner)} 🏆\n"
            f"Сыграли {game.round_number} {plural(game.round_number, 'раунд', 'раунда', 'раундов')}. "
            "Баллы сохранены — таблица лидеров пополнилась."
        ),
        color=COLOR_GOLD,
    )
    embed.add_field(name="🏅 Итоговая таблица", value=_leaderboard_rows(rows), inline=False)
    embed.set_footer(text="Спасибо за игру! Запустите /melody start, чтобы сыграть ещё.")
    return embed


def leaderboard_embed(
    rows: list[Any], guild: discord.Guild | None, *, title: str = "📊 Таблица лидеров"
) -> discord.Embed:
    embed = discord.Embed(title=title, color=COLOR_MAIN)
    embed.add_field(name="Баллы", value=_leaderboard_rows(rows, guild), inline=False)
    embed.set_footer(text="Баллы сохраняются между играми. Серия — победы подряд.")
    return embed


def host_panel_embed(game: GameState, selected_id: str | None = None) -> discord.Embed:
    round_ = game.round
    selected = None
    if round_ and selected_id:
        selected = next((t for t in round_.candidates if t.id == selected_id), None)
    embed = discord.Embed(
        title="🎛️ Панель ведущего",
        description=(
            "Выберите мелодию и длительность превью. Этот список видите только вы — "
            "игроки увидят результат после ставок."
        ),
        color=COLOR_MAIN,
    )
    embed.add_field(
        name="🎸 Мелодия",
        value=f"**{selected.label}** ({selected.duration_label})" if selected else "Не выбрана",
        inline=False,
    )
    embed.add_field(
        name="⏱ Длительность превью",
        value=f"{round_.snippet_sec if round_ else game.default_snippet_sec} с",
        inline=True,
    )
    embed.add_field(
        name="🔄 Обновления",
        value=f"{game.rerolls_left} {plural(game.rerolls_left, 'раз', 'раза', 'раз')} осталось",
        inline=True,
    )
    embed.add_field(name="👥 Игроки", value=_player_names(game), inline=False)
    embed.set_footer(text="После запуска раунда ставки будут открыты.")
    return embed


def wrong_guess_embed(outcome: GuessOutcome) -> discord.Embed:
    embed = discord.Embed(
        title="❌ Не та мелодия",
        description=(
            f"Близость: **{outcome.score:.0f}%**.\n"
            f"Множитель снижен до **{fmt_multiplier(outcome.multiplier)}**. "
            "Попробуйте ещё раз — время идёт!"
        ),
        color=COLOR_ORANGE,
    )
    embed.set_footer(text="Каждая ошибка отнимает 0.25 у множителя.")
    return embed


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="Ошибка", description=str(message)[:1900], color=COLOR_DANGER)


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(description=message[:1900], color=COLOR_SUCCESS)


def cancel_embed(reason: str) -> discord.Embed:
    embed = discord.Embed(
        title="🛑 Игра отменена",
        description=reason or "Ведущий остановил игру.",
        color=COLOR_DANGER,
    )
    embed.add_field(
        name="💵 Ставки", value="Незавершённые ставки возвращены игрокам.", inline=False
    )
    return embed


def track_embed(track: Track, *, key: str = "melody") -> discord.Embed:
    embed = discord.Embed(
        title="✅ Трек добавлен",
        description=f"**{track.label[:300]}**",
        color=COLOR_SUCCESS,
    )
    embed.add_field(name="⏱ Длительность", value=track.duration_label, inline=True)
    embed.add_field(name="🆔 ID", value=f"`{track.id}`", inline=True)
    embed.add_field(name="📁 Плейлист", value=f"`{key}`", inline=True)
    embed.set_footer(text="Используйте /melody start, чтобы сыграть.")
    return embed


def help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Угадай мелодию — правила",
        description=(
            "Ведущий выбирает трек и длительность превью. Игроки ставят баллы, "
            "слушают фрагмент и пытаются угадать название или исполнителя. "
            "Первый верный ответ забирает банк."
        ),
        color=COLOR_MAIN,
    )
    embed.add_field(
        name="🎯 Множители",
        value=(
            "×3 — ответ в первые 25% времени\n"
            "×2 — до 50%\n"
            "×1.5 — до 75%\n"
            "×1.25 — до конца\n"
            "−0.25 за каждую ошибку\n"
            "+10% за победу подряд (максимум +50%)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎙 Ведущий",
        value=(
            "Выбирает трек и длительность, получает 15% банка.\n"
            "Роль ведущего переходит к следующему игроку каждый раунд."
        ),
        inline=False,
    )
    embed.add_field(
        name="💬 Команды",
        value=(
            "`/melody start` — начать игру\n"
            "`/melody bet <сумма>` — ставка\n"
            "`/melody guess <название>` — ответ\n"
            "`/melody host` — панель ведущего\n"
            "`/melody next` — следующий раунд\n"
            "`/melody top` — таблица лидеров\n"
            "`/melody profile` — статистика\n"
            "`/melody history` — последние раунды\n"
            "`/melody settings` — настройки (админ)\n"
            "`/playlist add` — добавить трек"
        ),
        inline=False,
    )
    embed.set_footer(text="Минимум 2 игрока. Для игры нужен голосовой канал.")
    return embed


def settings_embed(settings: GuildSettings) -> discord.Embed:
    embed = discord.Embed(title="⚙️ Настройки «Угадай мелодию»", color=COLOR_MAIN)
    fields = {
        "Раунды": str(settings.rounds),
        "Превью": f"{settings.snippet_sec} с",
        "Время на ставки": f"{settings.betting_seconds} с",
        "Стартовый баланс": f"{settings.starting_balance:,}".replace(",", " "),
        "Комиссия ведущего": f"{settings.host_fee_percent}%",
        "Минимальная ставка": str(settings.min_bet),
        "Случайное смещение": "вкл" if settings.random_offset else "выкл",
        "Плейлист": settings.playlist_display_name,
        "Роль ведущего": role_mention(settings.host_role_id)
        if settings.host_role_id
        else "не задана",
        "Текстовый канал": channel_mention(settings.text_channel_id)
        if settings.text_channel_id
        else "текущий",
        "Голосовой канал": channel_mention(settings.voice_channel_id)
        if settings.voice_channel_id
        else "текущий",
    }
    for name, value in fields.items():
        embed.add_field(name=name, value=value, inline=True)
    embed.set_footer(text="Изменить: /melody settings")
    return embed


def playlist_embed(
    key: str,
    name: str,
    tracks: list[Track],
    *,
    guild_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📁 Плейлист «{name[:100]}»",
        description=f"Ключ: `{key}` · треков: {len(tracks)}",
        color=COLOR_MAIN,
    )
    if not tracks:
        embed.add_field(
            name="Пусто",
            value="Добавьте треки командой `/playlist add`.",
            inline=False,
        )
    else:
        lines = [
            f"{index}. **{track.label}** ({track.duration_label}) · `{track.id}`"
            for index, track in enumerate(tracks[:15], start=1)
        ]
        embed.add_field(name="Треки", value="\n".join(lines)[:1000], inline=False)
        if len(tracks) > 15:
            embed.set_footer(text=f"Показаны первые 15 из {len(tracks)}.")
    return embed


def profile_embed(
    stats: dict[str, Any] | None,
    user: discord.User | discord.Member,
    *,
    starting_balance: int = 1000,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"👤 {getattr(user, 'display_name', None) or user.name}",
        color=COLOR_MAIN,
    )
    if stats is None:
        embed.description = (
            "Игрок ещё не участвовал в играх. Присоединитесь к игре — "
            f"на счёт начисляется {starting_balance:,} стартовых баллов.".replace(",", " ")
        )
        return embed
    games = int(stats.get("games") or 0)
    wins = int(stats.get("wins") or 0)
    correct = int(stats.get("correct_guesses") or 0)
    wrong = int(stats.get("wrong_guesses") or 0)
    rate = (wins / games * 100) if games else 0.0
    embed.add_field(
        name="💰 Баланс",
        value=f"**{int(stats.get('balance') or 0):,}**".replace(",", " "),
        inline=True,
    )
    embed.add_field(name="🎮 Игры", value=str(games), inline=True)
    embed.add_field(name="🏆 Победы", value=str(wins), inline=True)
    embed.add_field(name="📈 Винрейт", value=f"{rate:.0f}%", inline=True)
    embed.add_field(name="🎯 Угадано", value=str(correct), inline=True)
    embed.add_field(name="❌ Ошибок", value=str(wrong), inline=True)
    embed.add_field(name="🔥 Лучшая серия", value=str(stats.get("best_streak") or 0), inline=True)
    embed.add_field(
        name="💵 Всего выиграно",
        value=f"**{int(stats.get('total_won') or 0):,}**".replace(",", " "),
        inline=True,
    )
    embed.set_footer(text="Баллы сохраняются между играми.")
    return embed


def playlists_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(title="📁 Плейлисты", color=COLOR_MAIN)
    if not rows:
        embed.description = (
            "Пока нет плейлистов. Добавьте трек командой `/playlist add` — "
            "плейлист `melody` создастся автоматически."
        )
        return embed
    lines = [
        f"• `{row['key']}` — **{row['name']}** · {row['count']} "
        f"{plural(int(row['count']), 'трек', 'трека', 'треков')}"
        for row in rows
    ]
    embed.add_field(name="Список", value="\n".join(lines)[:1000], inline=False)
    embed.set_footer(text="Ключ используется в /melody settings playlist.")
    return embed


def history_embed(rows: list[dict[str, Any]], guild: discord.Guild | None) -> discord.Embed:
    embed = discord.Embed(title="🕰️ Последние раунды", color=COLOR_MAIN)
    if not rows:
        embed.description = "Пока нет сыгранных раундов."
        return embed
    lines = []
    for row in rows:
        track = row.get("track_title") or "—"
        artist = row.get("track_artist") or ""
        label = f"{artist} — {track}" if artist else str(track)
        winner_id = row.get("winner_id")
        winner = mention(int(winner_id)) if winner_id else "никто"
        multiplier = row.get("winner_multiplier")
        mult_text = fmt_multiplier(float(multiplier)) if multiplier else "—"
        lines.append(
            f"**#{row.get('round_number', '?')}** · {label[:70]} · угадал {winner} · {mult_text}"
        )
    embed.add_field(name="Раунды", value="\n".join(lines)[:1000], inline=False)
    embed.set_footer(text=f"Всего в истории: {len(rows)}")
    return embed
