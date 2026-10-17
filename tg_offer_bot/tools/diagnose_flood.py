"""Нормализует ли Telegram гомоглифы в антиспам-фильтре?

Сравниваем карточку с «Ѕtаrѕ» (кириллические омоглифы) и с «⭐»,
отправляя их вплотную друг за другом.
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

TARGET = 47460748


async def main() -> None:
    settings = get_settings()
    db = Database(settings.db_path)
    await db.connect()
    bcid = (await BusinessConnectionRepository(db).list_all())[0]["id"]
    await db.close()

    labels = ["Ѕtаrѕ", "⭐", "Ѕtаrѕ", "⭐"]
    async with Bot(settings.bot_token) as bot:
        for label in labels:
            card = templates.offer_card(
                seller_username="testuser",
                seller_name="Test User",
                gift_title="Star Notepad #21,211",
                gift_url="https://t.me/nft/StarNotepad-21211",
                stars=999,
                currency_label=label,
                remaining_seconds=21600,
            )
            try:
                m = await bot.send_message(
                    TARGET,
                    card,
                    business_connection_id=bcid,
                    reply_markup=inline.offer_keyboard("x" * 32),
                )
                print(f"label={label!r}: OK (msg {m.message_id})")
            except TelegramAPIError as exc:
                print(f"label={label!r}: {exc.message}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
