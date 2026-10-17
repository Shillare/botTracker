"""Актуальное состояние бизнес-подключений через Bot API.

Зачем это нужно
----------------
Telegram присылает ``business_connection`` **один раз** — в момент подключения.
Если бот в этот момент не работал (перезапуск, поллинг офлайн, свежая база),
событие потеряно навсегда: в базе записи нет, а апдейты продолжают приходить с
незнакомым ``business_connection_id``.

Раньше бот в такой ситуации молча откатывался на отправку от своего имени:
карточка уходила не от бизнес-аккаунта, команду удалить было нечем, а
``/business_info`` показывал «Подключений пока нет».

Восстанавливать запись из самого апдейта можно только гадая: права в нём нет.
Надёжный источник — метод Bot API ``getBusinessConnection``, который возвращает
полное состояние соединения вместе с ``rights``.

Побочная польза: только здесь видно **реальные** права. Если при подключении не
отметить «Разрешить отвечать на сообщения», отправка от имени аккаунта будет
отклоняться с ``BUSINESS_PEER_INVALID``, и без проверки это выглядит как
«бот сломался».
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection

from app.database import BusinessConnectionRepository

logger = logging.getLogger(__name__)


def connection_payload(connection: BusinessConnection) -> dict:
    """Разворачивает объект Bot API в строку таблицы ``business_connections``."""
    rights = connection.rights
    user = connection.user
    return {
        "id": connection.id,
        "user_id": user.id,
        "user_chat_id": connection.user_chat_id,
        "username": user.username,
        "first_name": user.first_name,
        "is_enabled": connection.is_enabled,
        "can_reply": bool(rights.can_reply) if rights else False,
        "can_read_messages": bool(rights.can_read_messages) if rights else False,
        "can_transfer_and_upgrade": bool(rights.can_transfer_and_upgrade_gifts) if rights else False,
        "can_transfer_stars": bool(rights.can_transfer_stars) if rights else False,
        "can_convert_gifts_to_stars": bool(rights.can_convert_gifts_to_stars) if rights else False,
        "connected_at": connection.date.isoformat(),
    }


def granted_rights(connection: BusinessConnection) -> list[str]:
    """Человеческие названия выданных прав — для логов и уведомлений."""
    rights = connection.rights
    if rights is None:
        return []
    names = {
        "can_reply": "отвечать на сообщения",
        "can_read_messages": "читать сообщения",
        "can_transfer_and_upgrade_gifts": "передавать и повышать подарки",
        "can_transfer_stars": "передавать Stars",
        "can_convert_gifts_to_stars": "конвертировать подарки в Stars",
    }
    return [label for flag, label in names.items() if getattr(rights, flag, False)]


async def refresh(
    bot: Bot | None,
    repository: BusinessConnectionRepository,
    connection_id: str,
) -> BusinessConnection | None:
    """Перезапрашивает соединение у Telegram и сохраняет его в базу.

    ``None`` — соединение недоступно: сети нет, идентификатор невалиден или
    аккаунт отключил бота. Вызывающий код решает сам, что делать.
    """
    if bot is None:
        return None
    try:
        connection = await bot.get_business_connection(connection_id)
    except TelegramAPIError as exc:
        logger.warning(
            "getBusinessConnection(%s) не сработал: %s", connection_id, exc
        )
        return None

    await repository.upsert(connection_payload(connection))
    rights = granted_rights(connection)
    if rights:
        logger.info(
            "Бизнес-подключение %s синхронизировано с Telegram: %s, права: %s",
            connection_id,
            f"@{connection.user.username}" if connection.user.username else connection.user.id,
            ", ".join(rights),
        )
    else:
        logger.warning(
            "Бизнес-подключение %s синхронизировано, но прав нет ни одного: "
            "Telegram не даст отправлять от имени аккаунта. Переподключите бота "
            "и отметьте «Разрешить отвечать на сообщения».",
            connection_id,
        )
    return connection


async def sync_all(
    bot: Bot | None,
    repository: BusinessConnectionRepository,
    connection_ids: list[str],
) -> dict[str, bool]:
    """Синхронизирует несколько подключений (используется при старте)."""
    result: dict[str, bool] = {}
    for connection_id in connection_ids:
        result[connection_id] = await refresh(bot, repository, connection_id) is not None
    return result


__all__ = ("connection_payload", "granted_rights", "refresh", "sync_all")