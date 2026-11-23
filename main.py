"""Запуск Biba-бота.

    python main.py

Токен берётся из переменной окружения ``DISCORD_TOKEN`` (или из ``.env``,
если вы используете dotenv-обёртку).
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from biba.bot import create_bot
from biba.config import Config


def main() -> None:
    # Для локального запуска достаточно .env; Docker Compose передаёт
    # переменные через env_file, и load_dotenv просто ничего не находит.
    load_dotenv()
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)

    bot = create_bot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
