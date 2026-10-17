"""Обработчики событий бизнес-подключения (``business_connection``)."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection

from app.database import BusinessConnectionRepository, Database

logger = logging.getLogger(__name__)


def rights_summary(connection: BusinessConnection) -> str:
    rights = connection.rights
    if rights is None:
        return "права не переданы"
    flags = {
        "reply": rights.can_reply,
        "read": rights.can_read_messages,
        "transfer_gifts": rights.can_transfer_and_upgrade_gifts,
        "transfer_stars": rights.can_transfer_stars,
        "convert_gifts": rights.can_convert_gifts_to_stars,
    }
    granted = [name for name, value in flags.items() if value]
    return ", ".join(granted) if granted else "нет прав на действия"


async def _notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление в чат %s", chat_id)


def create_business_router() -> Router:
    """Фабрика роутера бизнес-подключений."""
    router = Router(name="business")

    @router.business_connection()
    async def on_business_connection(event: BusinessConnection, bot: Bot, db: Database) -> None:
        """Сохраняет, активирует и отключает бизнес-подключение.

        Telegram присылает ``business_connection`` дважды за цикл:
        при подключении (``is_enabled=True``) и при отключении (``is_enabled=False``).
        """
        rights = event.rights
        user = event.user
        account = f"@{user.username}" if user.username else f"id={user.id}"

        await BusinessConnectionRepository(db).upsert(
            {
                "id": event.id,
                "user_id": user.id,
                "user_chat_id": event.user_chat_id,
                "username": user.username,
                "first_name": user.first_name,
                "is_enabled": event.is_enabled,
                "can_reply": bool(rights.can_reply) if rights else False,
                "can_read_messages": bool(rights.can_read_messages) if rights else False,
                "can_transfer_and_upgrade": bool(rights.can_transfer_and_upgrade_gifts) if rights else False,
                "can_transfer_stars": bool(rights.can_transfer_stars) if rights else False,
                "can_convert_gifts_to_stars": bool(rights.can_convert_gifts_to_stars) if rights else False,
                "connected_at": event.date.isoformat(),
            },
        )

        if event.is_enabled:
            rights_text = rights_summary(event)
            if rights is None or not any(
                (
                    rights.can_reply,
                    rights.can_read_messages,
                    rights.can_transfer_and_upgrade_gifts,
                    rights.can_transfer_stars,
                    rights.can_convert_gifts_to_stars,
                )
            ):
                # Подключение без единого права выглядит рабочим (бот пишет
                # «подключён»), но отправлять от имени аккаунта он не сможет:
                # Telegram отвечает BUSINESS_PEER_INVALID. Без предупреждения
                # продавец ищет причину в боте, а она в галочках при подключении.
                logger.warning(
                    "Бизнес-аккаунт ПОДКЛЮЧЁН БЕЗ ПРАВ (%s, connection_id=%s): отправка "
                    "от имени аккаунта работать не будет. Переподключите бота и отметьте "
                    "«Разрешить отвечать на сообщения».",
                    account,
                    event.id,
                )
            else:
                logger.info(
                    "Бизнес-аккаунт ПОДКЛЮЧЁН: %s, connection_id=%s, права: %s",
                    account,
                    event.id,
                    rights_text,
                )

            await _notify(
                bot,
                event.user_chat_id,
                "<b>Бизнес-аккаунт подключён.</b>\n\n"
                f"<code>{event.id}</code>\n"
                "Бот может отправлять сообщения от вашего имени.\n\n"
                f"Права: <i>{rights_text}</i>",
            )
        else:
            logger.info("Бизнес-аккаунт ОТКЛЮЧЁН: %s, connection_id=%s", account, event.id)
            await _notify(
                bot,
                event.user_chat_id,
                "<b>Бизнес-аккаунт отключён.</b>\n\n"
                "Сообщения отправляются от имени бота, а не от вашего аккаунта.",
            )

    return router
