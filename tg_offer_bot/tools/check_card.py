"""Проверка готовой карточки с кнопками против реального Telegram API.

Отправляет карточку так же, как это делает бот, и печатает результат.
Запуск: ``python tools/check_card.py [chat_id]``
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import get_settings
from app.database import BusinessConnectionRepository, Database
from app.keyboards import inline
from app.services import templates
from app.services.parser import parse_gift_link


async def main(chat_id: int) -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    connections = await BusinessConnectionRepository(db).list_all()
    await db.close()
    if not connections:
        print("Нет бизнес-подключений. Подключите бота к бизнес-аккаунту.")
        return

    bcid = connections[0]["id"]
    gift = parse_gift_link("https://t.me/nft/StarNotepad-21211")
    card = templates.offer_card(
        seller_username="testuser",
        seller_name="Test User",
        gift_title=gift.display_title,
        gift_url=gift.url,
        stars=999,
        currency_label=settings.currency_label("Stars"),
        remaining_seconds=settings.offer_ttl_seconds,
    )
    accepted = templates.accepted_card(
        gift_title=gift.display_title,
        currency_label=settings.currency_label("Stars"),
    )
    print("--- текст карточки ---")
    print(card)
    print("--- текст после «Принять» ---")
    print(accepted)
    print("--- кнопки ---")
    for row in inline.offer_keyboard("x" * 32).inline_keyboard:
        for button in row:
            print(
                f"  {button.text!r} style={button.style!r} "
                f"icon={button.icon_custom_emoji_id!r} data={button.callback_data!r}"
            )
    print("--- отправка ---")

    async with Bot(settings.bot_token) as bot:
        try:
            message = await bot.send_message(
                chat_id,
                card,
                business_connection_id=bcid,
                reply_markup=inline.offer_keyboard("x" * 32),
            )
        except TelegramAPIError as exc:
            print(f"ОТКАЗ: {exc.message}")
            return
        print(f"OK: сообщение {message.message_id} отправлено в чат {chat_id}")


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 47460748
    asyncio.run(main(target))
